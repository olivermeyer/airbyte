#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


import json
from datetime import datetime
from typing import Dict, Generator

from airbyte_cdk.logger import AirbyteLogger
from airbyte_cdk.models import (
    AirbyteCatalog,
    AirbyteConnectionStatus,
    AirbyteMessage,
    AirbyteRecordMessage,
    AirbyteStream,
    ConfiguredAirbyteCatalog,
    Status,
    Type,
)
from airbyte_cdk.sources import Source
from elasticsearch import Elasticsearch


class UnsupportedDataTypeException(Exception):
    pass


class SourceElasticsearch(Source):
    system_indices = [
        ".kibana_1",
        ".opendistro_security",
    ]

    es_to_json_type_mapping = {
        # TODO: support objects, arrays, etc
        # ES data types: https://www.elastic.co/guide/en/elasticsearch/reference/current/mapping-types.html
        # JSON data types: http://json-schema.org/understanding-json-schema/reference/type.html
        "boolean": "boolean",
        "text": "string",
        "date": "string",
        "date_nanos": "string",
        "long": "integer",
        "unsigned_long": "integer",
        "integer": "integer",
        "short": "integer",
        "byte": "integer",
        "double": "number",
        "float": "number",
        "half_float": "number",
        "scaled_float": "number",
    }

    def _get_es_client(self, config: json, logger: AirbyteLogger) -> Elasticsearch:
        """
        Returns an ElasticSearch client using the config.
        """
        logger.info(f"Creating ElasticSearch client for host {config['host']}")
        # TODO: support SSL
        return Elasticsearch(
            hosts=[config["host"]],
            http_auth=(config["username"], config["password"]),
        )

    def check(self, logger: AirbyteLogger, config: json) -> AirbyteConnectionStatus:
        """
        Creates an ES client and tries to ping it.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
        the properties of the spec.json file

        :return: AirbyteConnectionStatus indicating a Success or Failure
        """
        es = self._get_es_client(config, logger)
        if not es.ping():
            return AirbyteConnectionStatus(status=Status.FAILED, message="Connection failed")
        return AirbyteConnectionStatus(status=Status.SUCCEEDED)

    def discover(self, logger: AirbyteLogger, config: json) -> AirbyteCatalog:
        """
        Returns an AirbyteCatalog where each stream corresponds to an index in the ElasticSearch domain.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
        the properties of the spec.json file

        :return: AirbyteCatalog is an object describing a list of all available streams in this source.
            A stream is an AirbyteStream object that includes:
            - its stream name (or table name in the case of Postgres)
            - json_schema providing the specifications of expected schema for this stream (a list of columns described
            by their names and types)
        """
        streams = []
        es = self._get_es_client(config, logger)
        indices = self._get_indices(es, logger)
        for index_name in indices.keys():
            json_schema = {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": self._get_index_json_properties(es, index_name),
            }
            streams.append(
                # TODO: support incremental
                AirbyteStream(name=index_name, json_schema=json_schema, supported_sync_modes=["full_refresh"])
            )
        return AirbyteCatalog(streams=streams)

    @staticmethod
    def _get_indices(es: Elasticsearch, logger: AirbyteLogger) -> dict:
        """
        Returns all non-system indices in the domain.
        """
        logger.info("Getting indices from ElasticSearch")
        return {key: value for key, value in es.indices.get("*").items() if key not in SourceElasticsearch.system_indices}

    @staticmethod
    def _get_index_json_properties(es: Elasticsearch, index: str) -> dict:
        """
        Returns JSON-formatted properties for the index.

        Raises UnsupportedDataTypeException if the index contain an unsupported data type.
        """
        json_properties = {}
        es_properties = es.indices.get_mapping(index=index)[index]["mappings"]["properties"]
        for property_name, property_attributes in es_properties.items():
            # TODO: handle nested fields
            if "properties" in property_attributes:
                # If property_attributes contains a `properties` key, then we're dealing with a nested field.
                # Ignore this field until we handle nested objects.
                continue
            try:
                json_properties[property_name] = {"type": SourceElasticsearch.es_to_json_type_mapping[property_attributes["type"]]}
            except KeyError:
                raise UnsupportedDataTypeException(f"Unsupported data type: {property_attributes['type']}")
        return json_properties

    def read(
        self, logger: AirbyteLogger, config: json, catalog: ConfiguredAirbyteCatalog, state: Dict[str, any]
    ) -> Generator[AirbyteMessage, None, None]:
        """
        Returns a generator of the AirbyteMessages generated by reading the source with the given configuration,
        catalog, and state.

        :param logger: Logging object to display debug/info/error to the logs
            (logs will not be accessible via airbyte UI if they are not passed to this logger)
        :param config: Json object containing the configuration of this source, content of this json is as specified in
            the properties of the spec.json file
        :param catalog: The input catalog is a ConfiguredAirbyteCatalog which is almost the same as AirbyteCatalog
            returned by discover(), but
        in addition, it's been configured in the UI! For each particular stream and field, there may have been provided
        with extra modifications such as: filtering streams and/or columns out, renaming some entities, etc
        :param state: When a Airbyte reads data from a source, it might need to keep a checkpoint cursor to resume
            replication in the future from that saved checkpoint.
            This is the object that is provided with state from previous runs and avoid replicating the entire set of
            data everytime.

        :return: A generator that produces a stream of AirbyteRecordMessage contained in AirbyteMessage object.
        """
        # TODO: switch to ElasticSearch.search() with PIT
        # See https://www.elastic.co/guide/en/elasticsearch/reference/7.15/paginate-search-results.html#search-after
        es = self._get_es_client(config, logger)
        for configured_stream in catalog.streams:
            index_name = configured_stream.stream.name
            return self._scroll_through_index(es=es, index_name=index_name, page_size=config["page_size"], logger=logger)

    @staticmethod
    def _scroll_through_index(
        es: Elasticsearch, index_name: str, page_size: int, logger: AirbyteLogger
    ) -> Generator[AirbyteMessage, None, None]:
        logger.info(f"Scrolling through index {index_name}")
        page = es.search(
            index=index_name,
            body={"query": {"match_all": {}}},
            size=page_size,
            scroll="1m",
        )
        scroll_id = page["_scroll_id"]
        hits = page["hits"]["hits"]
        while hits:
            for hit in hits:
                yield AirbyteMessage(
                    type=Type.RECORD,
                    record=AirbyteRecordMessage(stream=index_name, data=hit["_source"], emitted_at=int(datetime.now().timestamp()) * 1000),
                )
            page = es.scroll(scroll_id=scroll_id, scroll="1m")
            scroll_id = page["_scroll_id"]
            hits = page["hits"]["hits"]
