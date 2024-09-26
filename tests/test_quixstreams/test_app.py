import contextlib
import logging
import os
import time
import uuid
from concurrent.futures import Future
from json import loads, dumps
from unittest.mock import patch, create_autospec

import pytest
from confluent_kafka import KafkaException, TopicPartition

from quixstreams.app import Application
from quixstreams.dataframe import StreamingDataFrame
from quixstreams.dataframe.windows.base import get_window_ranges
from quixstreams.exceptions import PartitionAssignmentError
from quixstreams.kafka.configuration import ConnectionConfig
from quixstreams.kafka.exceptions import KafkaConsumerException
from quixstreams.models import (
    DoubleDeserializer,
    DoubleSerializer,
    JSONDeserializer,
    SerializationError,
    JSONSerializer,
    TopicConfig,
)
from quixstreams.platforms.quix import QuixKafkaConfigsBuilder, QuixApplicationConfig
from quixstreams.platforms.quix.env import QuixEnvironment
from quixstreams.rowconsumer import RowConsumer
from quixstreams.rowproducer import RowProducer
from quixstreams.sinks import SinkBatch, SinkBackpressureError
from quixstreams.state import State
from quixstreams.sources import SourceException, multiprocessing
from tests.utils import DummySink, DummySource


def _stop_app_on_future(app: Application, future: Future, timeout: float):
    """
    Call "Application.stop" after the future is resolved to stop the poll loop
    """
    try:
        future.result(timeout)
    finally:
        app.stop()


def _stop_app_on_timeout(app: Application, timeout: float):
    """
    Call "Application.stop" after the timeout expires to stop the poll loop
    """
    time.sleep(timeout)
    app.stop()


class TestApplication:
    def test_produce_and_consume(self, app_factory, topic_factory):
        """
        Test that the basic producer can produce messages to a Kafka topic and the consumer
        can consume them.
        """
        total_messages = 3
        consumer_timeout_seconds = 10
        messages_to_produce = [
            {"key": f"key-{i}", "value": f"value-{i}"} for i in range(total_messages)
        ]

        app = app_factory(auto_offset_reset="earliest")
        topic_name, _ = topic_factory()

        # Produce messages
        with app.get_producer() as producer:
            for msg in messages_to_produce:
                producer.produce(
                    topic_name,
                    key=msg["key"].encode(),
                    value=msg["value"].encode(),
                )
            producer.flush()

        # Consume messages
        consumed_messages = []
        start_time = time.time()
        with app.get_consumer() as consumer:
            consumer.subscribe([topic_name])
            while (
                len(consumed_messages) < total_messages
                and time.time() - start_time < consumer_timeout_seconds
            ):
                msg = consumer.poll(timeout=5.0)
                if msg is not None and not msg.error():
                    consumed_messages.append(
                        {"key": msg.key().decode(), "value": msg.value().decode()}
                    )

        # Check that all messages have been produced and consumed correctly
        assert len(consumed_messages) == total_messages
        for msg in consumed_messages:
            assert msg in messages_to_produce

    def test_run_success(
        self,
        app_factory,
        row_consumer_factory,
        executor,
    ):
        """
        Test that StreamingDataFrame processes 3 messages from Kafka by having the
        app produce the consumed messages verbatim to a new topic, and of course
        committing the respective offsets after handling each message.
        """

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )

        partition_num = 0
        topic_in = app.topic(
            str(uuid.uuid4()),
            value_deserializer=JSONDeserializer(),
        )
        topic_out = app.topic(
            str(uuid.uuid4()),
            value_serializer=JSONSerializer(),
            value_deserializer=JSONDeserializer(),
        )
        sdf = app.dataframe(topic_in)
        sdf = sdf.to_topic(topic_out)

        processed_count = 0
        total_messages = 3
        # Produce messages to the topic and flush
        timestamp_ms = int(time.time() / 1000)
        headers = [("header", b"value")]
        data = {
            "key": b"key",
            "value": b'"value"',
            "partition": partition_num,
            "timestamp": timestamp_ms,
            "headers": headers,
        }
        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(topic_in.name, **data)

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 15.0)
        app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Ensure that the right offset is committed
        with row_consumer_factory(auto_offset_reset="latest") as row_consumer:
            committed, *_ = row_consumer.committed(
                [TopicPartition(topic_in.name, partition_num)]
            )
            assert committed.offset == total_messages

        # confirm messages actually ended up being produced by the app
        rows_out = []
        with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
            row_consumer.subscribe([topic_out])
            while len(rows_out) < total_messages:
                rows_out.append(row_consumer.poll_row(timeout=5))

        assert len(rows_out) == total_messages
        for row in rows_out:
            assert row.topic == topic_out.name
            assert row.key == data["key"]
            assert row.value == loads(data["value"].decode())
            assert row.timestamp == timestamp_ms
            assert row.headers == headers

    def test_run_fails_no_commit(
        self,
        app_factory,
        row_consumer_factory,
        executor,
    ):
        """
        Test that Application doesn't commit the checkpoint in case of failure
        """

        app = app_factory(
            auto_offset_reset="earliest",
            commit_interval=9999,  # Set a high commit interval to ensure no autocommit
        )

        partition_num = 0
        topic_in = app.topic(str(uuid.uuid4()))

        def count_and_fail(_):
            # Count the incoming messages and fail on processing the last one
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                raise ValueError("test")

        sdf = app.dataframe(topic_in).apply(count_and_fail)

        processed_count = 0
        total_messages = 3
        # Produce messages to the topic and flush
        data = {"key": b"key", "value": b'"value"', "partition": partition_num}
        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(topic_in.name, **data)

        failed = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, failed, 10.0)
        with pytest.raises(ValueError):
            app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Ensure the offset is not committed to Kafka
        with row_consumer_factory() as row_consumer:
            committed, *_ = row_consumer.committed(
                [TopicPartition(topic_in.name, partition_num)]
            )
        assert committed.offset == -1001

    def test_run_consumer_error_raised(self, app_factory, executor):
        # Set "auto_offset_reset" to "error" to simulate errors in Consumer
        app = app_factory(auto_offset_reset="error")
        topic = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        sdf = app.dataframe(topic)

        # Stop app after 10s if nothing failed
        executor.submit(_stop_app_on_timeout, app, 10.0)
        with pytest.raises(KafkaConsumerException):
            app.run(sdf)

    def test_run_deserialization_error_raised(self, app_factory, executor):
        app = app_factory(auto_offset_reset="earliest")
        topic = app.topic(str(uuid.uuid4()), value_deserializer=DoubleDeserializer())

        # Produce a string while double is expected
        with app.get_producer() as producer:
            producer.produce(topic=topic.name, value=b"abc")

        sdf = app.dataframe(topic)

        with pytest.raises(SerializationError):
            # Stop app after 10s if nothing failed
            executor.submit(_stop_app_on_timeout, app, 10.0)
            app.run(sdf)

    def test_run_consumer_error_suppressed(self, app_factory, executor):
        done = Future()
        polled = 0

        def on_consumer_error(exc, *args):
            nonlocal polled
            assert isinstance(exc, ValueError)
            polled += 1
            if polled > 1 and not done.done():
                done.set_result(True)
            return True

        app = app_factory(on_consumer_error=on_consumer_error)
        topic = app.topic(str(uuid.uuid4()))
        sdf = app.dataframe(topic)

        with patch.object(RowConsumer, "poll") as mocked:
            # Patch RowConsumer.poll to simulate failures
            mocked.side_effect = ValueError("test")
            # Stop app when the future is resolved
            executor.submit(_stop_app_on_future, app, done, 10.0)
            app.run(sdf)
        assert polled > 1

    def test_run_processing_error_raised(self, app_factory, executor):
        app = app_factory(auto_offset_reset="earliest")

        topic = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        sdf = app.dataframe(topic)

        def fail(*args):
            raise ValueError("test")

        sdf = sdf.apply(fail)

        with app.get_producer() as producer:
            producer.produce(topic=topic.name, value=b'{"field":"value"}')

        with pytest.raises(ValueError):
            executor.submit(_stop_app_on_timeout, app, 10.0)
            app.run(sdf)

    def test_run_processing_error_suppressed(self, app_factory, executor):
        produced = 2
        consumed = 0
        done = Future()

        def on_processing_error(exc, *args):
            nonlocal consumed
            assert isinstance(exc, ValueError)
            consumed += 1
            if consumed == produced:
                done.set_result(True)
            return True

        app = app_factory(
            auto_offset_reset="earliest", on_processing_error=on_processing_error
        )
        topic = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        sdf = app.dataframe(topic)

        def fail(*args):
            raise ValueError("test")

        sdf = sdf.apply(fail)

        with app.get_producer() as producer:
            for i in range(produced):
                producer.produce(topic=topic.name, value=b'{"field":"value"}')

        # Stop app from the background thread when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        assert produced == consumed

    def test_run_producer_error_raised(self, app_factory, producer, executor):
        app = app_factory(
            auto_offset_reset="earliest",
            producer_extra_config={"message.max.bytes": 1000},
        )

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        topic_out = app.topic(str(uuid.uuid4()), value_serializer=JSONSerializer())
        app._topic_manager.create_all_topics()

        sdf = app.dataframe(topic_in)
        sdf = sdf.to_topic(topic_out)

        # use separate producer instance which won't share extra_config
        with producer:
            producer.produce(topic_in.name, dumps({"field": 1001 * "a"}))

        with pytest.raises(KafkaException):
            executor.submit(_stop_app_on_timeout, app, 10.0)
            app.run(sdf)

    def test_run_serialization_error_raised(self, app_factory, executor):
        app = app_factory(auto_offset_reset="earliest")

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        topic_out = app.topic(str(uuid.uuid4()), value_serializer=DoubleSerializer())

        sdf = app.dataframe(topic_in)
        sdf = sdf.to_topic(topic_out)

        with app.get_producer() as producer:
            producer.produce(topic_in.name, b'{"field":"value"}')

        with pytest.raises(SerializationError):
            executor.submit(_stop_app_on_timeout, app, 10.0)
            app.run(sdf)

    def test_run_producer_error_suppressed(self, app_factory, executor):
        produce_input = 2
        produce_output_attempts = 0
        done = Future()

        def on_producer_error(exc, *args):
            nonlocal produce_output_attempts
            assert isinstance(exc, SerializationError)
            produce_output_attempts += 1
            if produce_output_attempts == produce_input:
                done.set_result(True)
            return True

        app = app_factory(
            auto_offset_reset="earliest", on_producer_error=on_producer_error
        )
        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())
        topic_out = app.topic(str(uuid.uuid4()), value_serializer=DoubleSerializer())

        sdf = app.dataframe(topic_in)
        sdf = sdf.to_topic(topic_out)

        with app.get_producer() as producer:
            for _ in range(produce_input):
                producer.produce(topic_in.name, b'{"field":"value"}')

        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)

        assert produce_output_attempts == produce_input

    def test_topic_init(self):
        app = Application(broker_address="localhost", consumer_group="test")
        topic = app.topic(name="test-topic")

        assert topic

    def test_streamingdataframe_init(self):
        app = Application(broker_address="localhost", consumer_group="test")
        topic = app.topic(name="test-topic")
        sdf = app.dataframe(topic)
        assert isinstance(sdf, StreamingDataFrame)

    def test_topic_auto_create_true(self, app_factory):
        """
        Topics are auto-created when auto_create_topics=True
        """
        app = app_factory(auto_create_topics=True)
        topic_manager = app._topic_manager
        _ = [app.topic("topic_in"), app.topic("topic_out")]

        with patch.object(topic_manager, "create_all_topics") as create:
            with patch.object(topic_manager, "validate_all_topics"):
                app.setup_topics()

        create.assert_called()

    def test_topic_auto_create_false(self, app_factory):
        """
        Topics are not auto-created when auto_create_topics=False
        """
        app = app_factory(auto_create_topics=False)
        topic_manager = app._topic_manager
        _ = [app.topic("topic_in"), app.topic("topic_out")]

        with patch.object(topic_manager, "create_all_topics") as create:
            with patch.object(topic_manager, "validate_all_topics"):
                app.setup_topics()

        create.assert_not_called()

    def test_topic_validation(self, app_factory):
        """
        Topics are validated
        """
        app = app_factory()
        topic_manager = app._topic_manager

        with patch.object(topic_manager, "validate_all_topics") as validate:
            with patch.object(topic_manager, "create_all_topics"):
                app.setup_topics()

        validate.assert_called()

    def test_topic_setup_on_get_producer(self, app_factory):
        """
        Topics are set up according to app settings when get_producer is called
        """
        app = app_factory()
        with patch.object(app, "setup_topics") as setup_topics:
            with app.get_producer():
                ...
        setup_topics.assert_called()

    def test_topic_setup_on_get_consumer(self, app_factory):
        """
        Topics are set up according to app settings when get_consumer is called
        """
        app = app_factory()
        with patch.object(app, "setup_topics") as setup_topics:
            with app.get_consumer():
                ...
        setup_topics.assert_called()

    def test_consumer_extra_config(self, app_factory):
        """
        Some configs like `enable.auto.offset.store` are overridable and others are not
        """
        app = app_factory(
            auto_offset_reset="latest",
            consumer_extra_config={
                "auto.offset.reset": "earliest",
                "enable.auto.offset.store": True,
            },
        )

        with app.get_consumer() as x:
            assert x._consumer_config["enable.auto.offset.store"] is True
            assert x._consumer_config["auto.offset.reset"] is "latest"

    def test_producer_extra_config(self, app_factory):
        """
        Test that producer receives the Application extra configs
        """
        app = app_factory(
            producer_extra_config={"linger.ms": 10},
        )

        with app.get_producer() as x:
            assert x._producer_config["linger.ms"] == 10

    def test_missing_broker_id_raise(self):
        # confirm environment is empty
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError) as e_info:
                Application()
            error_str = 'Either "broker_address" or "quix_sdk_token" must be provided'
            assert error_str in e_info.value.args

    def test_consumer_group_env(self):
        """
        Sanity check consumer_group gets set from the environment via getenv.
        """
        consumer_group = "my_group"
        with patch.dict(os.environ, {"Quix__Consumer__group": consumer_group}):
            app = Application(
                broker_address="my_address", consumer_group=consumer_group
            )
        assert app.config.consumer_group == consumer_group

    def test_consumer_group_default(self):
        """
        Sanity check behavior around getenv defaults
        """
        with patch.dict(os.environ, {}, clear=True):
            app = Application(broker_address="my_address")
        assert app.config.consumer_group == "quixstreams-default"


class TestAppGroupBy:

    def test_group_by(
        self,
        app_factory,
        row_consumer_factory,
        executor,
    ):
        """
        Test that StreamingDataFrame processes 6 messages from Kafka and groups them
        by each record's specified column value.
        """

        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        processed_count = 0

        timestamp_ms = int(time.time() * 1000)
        user_id = "abc123"
        value_in = {"user": user_id}
        expected_message_count = 1
        total_messages = expected_message_count * 2  # groupby reproduces each message
        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )

        app_topic_in = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )
        app_topic_out = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )

        sdf = app.dataframe(topic=app_topic_in)
        sdf = sdf.group_by("user")
        # Capture original message timestamp to ensure it's forwarded
        # to the repartition topic
        sdf["groupby_timestamp"] = sdf.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf = sdf.to_topic(app_topic_out)

        with app.get_producer() as producer:
            msg = app_topic_in.serialize(
                key="some_key", value=value_in, timestamp_ms=timestamp_ms
            )
            producer.produce(
                app_topic_in.name, key=msg.key, value=msg.value, timestamp=msg.timestamp
            )

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Consume the message from the output topic
        with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
            row_consumer.subscribe([app_topic_out])
            row = row_consumer.poll_row(timeout=5)

        # Check that "user_id" is now used as a message key
        assert row.key.decode() == user_id
        # Check that message timestamp of the repartitioned message is the same
        # as original one
        assert row.value == {
            "user": user_id,
            "groupby_timestamp": timestamp_ms,
        }

    @pytest.mark.parametrize("processing_guarantee", ["exactly-once", "at-least-once"])
    def test_group_by_with_window(
        self,
        app_factory,
        row_consumer_factory,
        executor,
        processing_guarantee,
    ):
        """
        Test that StreamingDataFrame processes 6 messages from Kafka and groups them
        by each record's specified column value.
        """

        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        processed_count = 0

        window_duration_ms = 1000
        timestamp_ms = int(time.time() * 1000)
        # use a "window-friendly" timestamp for easier testing
        timestamp_ms = timestamp_ms - (timestamp_ms % window_duration_ms)
        user_id = "abc123"
        value_in = {"user": user_id}
        expected_message_count = 1
        total_messages = expected_message_count * 2  # groupby reproduces each message
        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
            processing_guarantee=processing_guarantee,
        )

        app_topic_in = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )
        app_topic_out = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )

        sdf = app.dataframe(topic=app_topic_in)
        sdf = sdf.group_by("user")
        # Capture original message timestamp to ensure it's forwarded
        # to the repartition topic
        sdf["groupby_timestamp"] = sdf.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf = sdf.tumbling_window(duration_ms=window_duration_ms).count().current()
        sdf = sdf.to_topic(app_topic_out)

        with app.get_producer() as producer:
            msg = app_topic_in.serialize(
                key="some_key", value=value_in, timestamp_ms=timestamp_ms
            )
            producer.produce(
                app_topic_in.name, key=msg.key, value=msg.value, timestamp=msg.timestamp
            )

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Consume the message from the output topic
        with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
            row_consumer.subscribe([app_topic_out])
            row = row_consumer.poll_row(timeout=5)

        # Check that "user_id" is now used as a message key
        assert row.key.decode() == user_id
        # Check that window is calculated based on the original timestamp
        assert row.value == {
            "start": timestamp_ms,
            "end": timestamp_ms + window_duration_ms,
            "value": 1,
        }


class TestAppExactlyOnce:

    def test_exactly_once(
        self,
        app_factory,
        topic_manager_factory,
        row_consumer_factory,
        executor,
    ):
        """
        An Application that forwards messages to a new topic crashes after producing 2
        messages, then restarts (will reprocess all 3 messages again).

        The second run succeeds in processing all 3 messages and commits transaction.

        The 2 non-committed produces should be ignored by a downstream consumer.
        """
        processed_count = 0
        total_messages = 3
        fail_idx = 1
        done = Future()

        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count
            processed_count += 1
            # Stop processing after consuming all the messages
            # if (processed_count % total_messages) == 0:
            if processed_count == total_messages:
                done.set_result(True)

        class ForceFail(Exception): ...

        def fail_once(value):
            if processed_count == fail_idx:
                # sleep here to ensure produced messages actually make it to topic
                time.sleep(2)
                raise ForceFail
            return value

        consumer_group = str(uuid.uuid4())
        topic_in_name = str(uuid.uuid4())
        topic_out_name = str(uuid.uuid4())

        def get_app(fail: bool):
            app = app_factory(
                commit_interval=30,
                auto_offset_reset="earliest",
                on_message_processed=on_message_processed,
                consumer_group=consumer_group,
                processing_guarantee="exactly-once",
            )
            topic_in = app.topic(topic_in_name, value_deserializer="json")
            topic_out = app.topic(topic_out_name, value_serializer="json")
            sdf = app.dataframe(topic_in)
            sdf = sdf.to_topic(topic_out)
            if fail:
                sdf = sdf.apply(fail_once)
            return app, sdf, topic_in, topic_out

        # first run of app that encounters an error during processing
        app, sdf, topic_in, topic_out = get_app(fail=True)

        # produce initial messages to consume
        with app.get_producer() as producer:
            for i in range(total_messages):
                msg = topic_in.serialize(key=str(i), value={"my_val": str(i)})
                producer.produce(topic=topic_in.name, key=msg.key, value=msg.value)

        with pytest.raises(ForceFail):
            app.run(sdf)
        assert processed_count == fail_idx

        # re-init the app, only this time it won't fail
        processed_count = 0
        app, sdf, topic_in, topic_out = get_app(fail=False)
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)

        # only committed messages are read by a downstream consumer
        with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
            row_consumer.subscribe([topic_out])
            rows = []
            while (row := row_consumer.poll_row(timeout=5)) is not None:
                rows.append(row)
            lowwater, highwater = row_consumer.get_watermark_offsets(
                TopicPartition(topic_out.name, 0), 3
            )
        assert len(rows) == total_messages

        # Sanity check that non-committed messages actually made it to topic
        assert lowwater == 0
        # The first message being at offset 3 affirms the first transaction
        # was successfully aborted, as expected.
        assert rows[0].offset == fail_idx + 2 == 3
        # The last message being at offset 7 affirms the second transaction
        # was successfully committed, as expected.
        assert highwater == rows[-1].offset + 2 == 7


class TestQuixApplication:
    def test_init_with_quix_sdk_token_arg(self):
        consumer_group = "c_group"
        expected_workspace_cgroup = f"my_ws-{consumer_group}"
        quix_sdk_token = "my_sdk_token"
        quix_extras = {"quix": "extras"}
        extra_config = {"extra": "config"}
        connection_config = ConnectionConfig.from_librdkafka_dict(
            {
                "bootstrap.servers": "address1,address2",
                "sasl.mechanisms": "SCRAM-SHA-256",
                "security.protocol": "SASL_SSL",
                "sasl.username": "my-username",
                "sasl.password": "my-password",
                "ssl.ca.location": "/mock/dir/ca.cert",
            }
        )
        expected_producer_extra_config = {
            "enable.idempotence": True,
            **extra_config,
            **quix_extras,
        }
        expected_consumer_extra_config = {
            **extra_config,
            **quix_extras,
        }

        def get_cfg_builder(quix_sdk_token):
            cfg_builder = create_autospec(QuixKafkaConfigsBuilder)
            cfg_builder.workspace_id = "abcd"
            cfg_builder.librdkafka_connection_config = connection_config
            cfg_builder.prepend_workspace_id.return_value = expected_workspace_cgroup
            cfg_builder.quix_sdk_token = quix_sdk_token
            cfg_builder.get_application_config.side_effect = lambda cg: (
                QuixApplicationConfig(
                    connection_config, quix_extras, cfg_builder.prepend_workspace_id(cg)
                )
            )
            return cfg_builder

        # Mock consumer and producer to check the init args
        with patch("quixstreams.app.QuixKafkaConfigsBuilder", get_cfg_builder), patch(
            "quixstreams.app.RowConsumer"
        ) as consumer_init_mock, patch(
            "quixstreams.app.RowProducer"
        ) as producer_init_mock:
            app = Application(
                consumer_group=consumer_group,
                quix_sdk_token=quix_sdk_token,
                consumer_extra_config=extra_config,
                producer_extra_config=extra_config,
            )
            assert app.is_quix_app

        # Check if items from the Quix config have been passed
        # to the low-level configs of producer and consumer
        producer_call_kwargs = producer_init_mock.call_args.kwargs
        assert producer_call_kwargs["broker_address"] == connection_config
        assert producer_call_kwargs["extra_config"] == expected_producer_extra_config

        consumer_call_kwargs = consumer_init_mock.call_args.kwargs
        assert consumer_call_kwargs["broker_address"] == connection_config
        assert consumer_call_kwargs["consumer_group"] == expected_workspace_cgroup
        assert consumer_call_kwargs["extra_config"] == expected_consumer_extra_config

    def test_init_with_quix_sdk_token_env(self, monkeypatch):
        consumer_group = "c_group"
        expected_workspace_cgroup = f"my_ws-{consumer_group}"
        quix_sdk_token = "my_sdk_token"
        extra_config = {"extra": "config"}
        quix_extras = {"quix": "extras"}
        connection_config = ConnectionConfig.from_librdkafka_dict(
            {
                "bootstrap.servers": "address1,address2",
                "sasl.mechanisms": "SCRAM-SHA-256",
                "security.protocol": "SASL_SSL",
                "sasl.username": "my-username",
                "sasl.password": "my-password",
                "ssl.ca.location": "/mock/dir/ca.cert",
            }
        )
        expected_producer_extra_config = {
            "enable.idempotence": True,
            **extra_config,
            **quix_extras,
        }
        expected_consumer_extra_config = {
            **extra_config,
            **quix_extras,
        }

        def get_cfg_builder(quix_sdk_token):
            cfg_builder = create_autospec(QuixKafkaConfigsBuilder)
            cfg_builder.workspace_id = "abcd"
            cfg_builder.librdkafka_connection_config = connection_config
            cfg_builder.prepend_workspace_id.return_value = expected_workspace_cgroup
            cfg_builder.quix_sdk_token = quix_sdk_token
            cfg_builder.get_application_config.side_effect = lambda cg: (
                QuixApplicationConfig(
                    connection_config, quix_extras, cfg_builder.prepend_workspace_id(cg)
                )
            )
            return cfg_builder

        monkeypatch.setenv("Quix__Sdk__Token", quix_sdk_token)
        with patch("quixstreams.app.QuixKafkaConfigsBuilder", get_cfg_builder), patch(
            "quixstreams.app.RowConsumer"
        ) as consumer_init_mock, patch(
            "quixstreams.app.RowProducer"
        ) as producer_init_mock:
            Application(
                consumer_group=consumer_group,
                consumer_extra_config=extra_config,
                producer_extra_config=extra_config,
            )

        # Check if items from the Quix config have been passed
        # to the low-level configs of producer and consumer
        producer_call_kwargs = producer_init_mock.call_args.kwargs
        assert producer_call_kwargs["broker_address"] == connection_config
        assert producer_call_kwargs["extra_config"] == expected_producer_extra_config

        consumer_call_kwargs = consumer_init_mock.call_args.kwargs
        assert consumer_call_kwargs["broker_address"] == connection_config
        assert consumer_call_kwargs["consumer_group"] == expected_workspace_cgroup
        assert consumer_call_kwargs["extra_config"] == expected_consumer_extra_config

    def test_init_with_quix_config_builder(self):
        consumer_group = "c_group"
        expected_workspace_cgroup = f"my_ws-{consumer_group}"
        quix_sdk_token = "my_sdk_token"
        extra_config = {"extra": "config"}
        quix_extras = {"quix": "extras"}
        connection_config = ConnectionConfig.from_librdkafka_dict(
            {
                "bootstrap.servers": "address1,address2",
                "sasl.mechanisms": "SCRAM-SHA-256",
                "security.protocol": "SASL_SSL",
                "sasl.username": "my-username",
                "sasl.password": "my-password",
                "ssl.ca.location": "/mock/dir/ca.cert",
            }
        )
        expected_producer_extra_config = {
            "enable.idempotence": True,
            **extra_config,
            **quix_extras,
        }
        expected_consumer_extra_config = {
            **extra_config,
            **quix_extras,
        }

        def get_cfg_builder(quix_sdk_token):
            cfg_builder = create_autospec(QuixKafkaConfigsBuilder)
            cfg_builder.workspace_id = "abcd"
            cfg_builder.librdkafka_connection_config = connection_config
            cfg_builder.prepend_workspace_id.return_value = expected_workspace_cgroup
            cfg_builder.quix_sdk_token = quix_sdk_token
            cfg_builder.get_application_config.side_effect = lambda cg: (
                QuixApplicationConfig(
                    connection_config, quix_extras, cfg_builder.prepend_workspace_id(cg)
                )
            )
            return cfg_builder

        with patch("quixstreams.app.RowConsumer") as consumer_init_mock, patch(
            "quixstreams.app.RowProducer"
        ) as producer_init_mock:
            Application(
                consumer_group=consumer_group,
                quix_config_builder=get_cfg_builder(quix_sdk_token),
                consumer_extra_config=extra_config,
                producer_extra_config=extra_config,
            )

        # Check if items from the Quix config have been passed
        # to the low-level configs of producer and consumer
        producer_call_kwargs = producer_init_mock.call_args.kwargs
        assert producer_call_kwargs["broker_address"] == connection_config
        assert producer_call_kwargs["extra_config"] == expected_producer_extra_config

        consumer_call_kwargs = consumer_init_mock.call_args.kwargs
        assert consumer_call_kwargs["broker_address"] == connection_config
        assert consumer_call_kwargs["consumer_group"] == expected_workspace_cgroup
        assert consumer_call_kwargs["extra_config"] == expected_consumer_extra_config

    def test_init_with_broker_id_dont_raises(self):
        cfg_builder = create_autospec(QuixKafkaConfigsBuilder)
        cfg_builder.workspace_id = "abcd"

        app = Application(
            broker_address="address",
            quix_config_builder=cfg_builder,
        )

        assert not app.is_quix_app

    def test_topic_name_and_config(self, quix_app_factory):
        """
        Topic names created with Quix API have workspace id prefixed
        Topic config has provided values else defaults
        """
        workspace_id = "my-workspace"
        app = quix_app_factory(workspace_id=workspace_id)
        topic_manager = app._topic_manager
        initial_topic_name = "input_topic"
        topic_partitions = 5
        topic = app.topic(
            initial_topic_name,
            config=topic_manager.topic_config(num_partitions=topic_partitions),
        )
        expected_name = f"{workspace_id}-{initial_topic_name}"
        expected_topic = topic_manager.topics[expected_name]
        assert topic.name == expected_name
        assert expected_name in topic_manager.topics
        assert (
            expected_topic.config.replication_factor
            == topic_manager.default_replication_factor
        )
        assert expected_topic.config.num_partitions == topic_partitions


class TestQuixApplicationWithState:
    def test_quix_app_no_state_management_warning(
        self, quix_app_factory, monkeypatch, topic_factory, executor
    ):
        """
        Ensure that Application.run() prints a warning if the app is stateful,
        runs on Quix (the "Quix__Deployment__Id" env var is set),
        but the "State Management" flag is disabled for the deployment.
        """
        app = quix_app_factory()
        topic = app.topic(str(uuid.uuid4()))
        sdf = app.dataframe(topic)
        sdf = sdf.apply(lambda x, state: x, stateful=True)

        monkeypatch.setenv(
            QuixEnvironment.DEPLOYMENT_ID,
            "123",
        )
        monkeypatch.setenv(
            QuixEnvironment.STATE_MANAGEMENT_ENABLED,
            "",
        )

        with pytest.warns(RuntimeWarning) as warned:
            executor.submit(_stop_app_on_timeout, app, 5.0)
            app.run(sdf)

        warnings = [w for w in warned.list if w.category is RuntimeWarning]
        warning = str(warnings[0].message)
        assert "State Management feature is disabled" in warning

    def test_quix_app_state_dir_mismatch_warning(
        self, quix_app_factory, monkeypatch, caplog
    ):
        """
        Ensure that Application.Quix() logs a warning
        if the app runs on Quix (the "Quix__Deployment__Id" env var is set),
        but the "state_dir" path doesn't match the one on Quix.
        """
        monkeypatch.setenv(
            QuixEnvironment.DEPLOYMENT_ID,
            "123",
        )
        with pytest.warns(RuntimeWarning) as warned:
            quix_app_factory()
        warnings = [w for w in warned.list if w.category is RuntimeWarning]
        warning = str(warnings[0].message)
        assert "does not match the state directory" in warning


class TestApplicationWithState:
    def test_run_stateful_success(
        self,
        app_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        """
        Test that StreamingDataFrame processes 3 messages from Kafka and updates
        the counter in the state store
        """

        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        partition_num = 0
        app = app_factory(
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
        )

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())

        # Define a function that counts incoming Rows using state
        def count(_, state: State):
            total = state.get("total", 0)
            total += 1
            state.set("total", total)
            if total == total_messages:
                total_consumed.set_result(total)

        sdf = app.dataframe(topic_in)
        sdf = sdf.update(count, stateful=True)

        total_messages = 3
        # Produce messages to the topic and flush
        message_key = b"key"
        data = {
            "key": message_key,
            "value": dumps({"key": "value"}),
            "partition": partition_num,
        }
        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(topic_in.name, **data)

        total_consumed = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, total_consumed, 10.0)
        app.run(sdf)

        # Check that the values are actually in the DB
        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )
        state_manager.register_store(topic_in.name, "default")
        state_manager.on_partition_assign(
            topic=topic_in.name, partition=partition_num, committed_offset=-1001
        )
        store = state_manager.get_store(topic=topic_in.name, store_name="default")
        with store.start_partition_transaction(partition=partition_num) as tx:
            # All keys in state must be prefixed with the message key
            assert tx.get("total", prefix=message_key) == total_consumed.result()

    def test_run_stateful_fails_no_commit(
        self,
        app_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        app = app_factory(
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
            commit_interval=9999,  # Set a high commit interval to ensure no autocommit
        )

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())

        # Define a function that counts incoming Rows using state
        def count_and_fail(_, state: State):
            total = state.get("total", 0)
            total += 1
            state.set("total", total)
            # Fail after processing all messages
            if total == total_messages:
                raise ValueError("test")

        failed = Future()

        sdf = app.dataframe(topic_in).update(count_and_fail, stateful=True)

        total_messages = 3
        # Produce messages to the topic and flush
        key = b"key"
        value = dumps({"key": "value"})

        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(topic_in.name, key=key, value=value)

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, failed, 10.0)
        with pytest.raises(ValueError):
            app.run(sdf)

        # Ensure that nothing was committed to the DB
        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )
        state_manager.register_store(topic_in.name, "default")
        state_manager.on_partition_assign(
            topic=topic_in.name, partition=0, committed_offset=-1001
        )
        store = state_manager.get_store(topic=topic_in.name, store_name="default")
        with store.start_partition_transaction(partition=0) as tx:
            assert tx.get("total", prefix=key) is None

    def test_run_stateful_suppress_processing_errors(
        self,
        app_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        partition_num = 0
        app = app_factory(
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
            # Suppress errors during message processing
            on_processing_error=lambda *args: True,
        )

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())

        # Define a function that counts incoming Rows using state
        def count(_, state: State):
            total = state.get("total", 0)
            total += 1
            state.set("total", total)
            if total == total_messages:
                total_consumed.set_result(total)

        def fail(_):
            raise ValueError("test")

        sdf = app.dataframe(topic_in).update(count, stateful=True).apply(fail)

        total_messages = 3
        message_key = b"key"
        # Produce messages to the topic and flush
        data = {
            "key": message_key,
            "value": dumps({"key": "value"}),
            "partition": partition_num,
        }
        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(topic_in.name, **data)

        total_consumed = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, total_consumed, 10.0)
        # Run the application
        app.run(sdf)

        # Ensure that data is committed to the DB
        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )
        state_manager.register_store(topic_in.name, "default")
        state_manager.on_partition_assign(
            topic=topic_in.name, partition=partition_num, committed_offset=-1001
        )
        store = state_manager.get_store(topic=topic_in.name, store_name="default")
        with store.start_partition_transaction(partition=partition_num) as tx:
            assert tx.get("total", prefix=message_key) == total_consumed.result()

    def test_on_assign_topic_offset_behind_warning(
        self,
        app_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        partition_num = 0
        app = app_factory(
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
        )

        topic_in = app.topic(str(uuid.uuid4()), value_deserializer=JSONDeserializer())

        # Set the store partition offset to 9999
        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )
        with state_manager:
            state_manager.register_store(topic_in.name, "default")
            state_partitions = state_manager.on_partition_assign(
                topic=topic_in.name, partition=partition_num, committed_offset=-1001
            )
            store = state_manager.get_store(topic_in.name, "default")
            tx = store.start_partition_transaction(partition_num)
            # Do some change to probe the Writebatch
            tx.set("key", "value", prefix=b"__key__")
            tx.flush(processed_offset=9999)
            assert state_partitions[partition_num].get_processed_offset() == 9999

        # Define some stateful function so the App assigns store partitions
        done = Future()

        sdf = app.dataframe(topic_in).update(
            lambda *_: done.set_result(True), stateful=True
        )

        # Produce a message to the topic and flush
        data = {
            "key": b"key",
            "value": dumps({"key": "value"}),
            "partition": partition_num,
        }
        with app.get_producer() as producer:
            producer.produce(topic_in.name, **data)

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        # Run the application
        with patch.object(logging.getLoggerClass(), "warning") as mock:
            app.run(sdf)

        assert mock.called
        assert "is behind the stored offset" in mock.call_args[0][0]

    def test_clear_state(
        self,
        app_factory,
        producer,
        topic_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        """
        Test that clear_state() removes all data from the state stores
        """

        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        app = app_factory(
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
        )

        topic_in_name, _ = topic_factory()
        prefix = b"key"

        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )

        # Add data to the state store
        with state_manager:
            state_manager.register_store(topic_in_name, "default")
            state_manager.on_partition_assign(
                topic=topic_in_name, partition=0, committed_offset=-1001
            )
            store = state_manager.get_store(topic=topic_in_name, store_name="default")
            with store.start_partition_transaction(partition=0) as tx:
                # All keys in state must be prefixed with the message key
                tx.set(key="my_state", value=True, prefix=prefix)

        # Clear the state
        app.clear_state()

        # Check that the date is cleared from the state store
        with state_manager:
            state_manager.register_store(topic_in_name, "default")
            state_manager.on_partition_assign(
                topic=topic_in_name, partition=0, committed_offset=-1001
            )
            store = state_manager.get_store(topic=topic_in_name, store_name="default")
            with store.start_partition_transaction(partition=0) as tx:
                assert tx.get("my_state", prefix=prefix) is None

    def test_app_use_changelog_false(self, app_factory):
        """
        `Application`s StateStoreManager should not have a TopicManager if
        use_changelog_topics is set to `False`.
        """
        app = app_factory(use_changelog_topics=False)
        assert not app._state_manager.using_changelogs


class TestApplicationRecovery:
    def test_changelog_recovery_default_store(
        self,
        app_factory,
        executor,
        tmp_path,
        state_manager_factory,
    ):
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        topic_name = str(uuid.uuid4())
        sum_key = "my_sum"
        store_name = "default"
        msg_int_value = 10
        processed_count = {0: 0, 1: 0}
        partition_msg_count = {0: 4, 1: 2}

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            processed_count[partition] += 1
            if processed_count == partition_msg_count:
                done.set_result(True)

        def sum_value(value: dict, state: State):
            new_value = state.get(sum_key, 0) + value["my_value"]
            state.set(sum_key, new_value)
            return new_value

        def get_app():
            app = app_factory(
                commit_interval=0,  # Commit every processed message
                auto_offset_reset="earliest",
                use_changelog_topics=True,
                on_message_processed=on_message_processed,
                consumer_group=consumer_group,
                state_dir=state_dir,
            )
            topic = app.topic(
                topic_name,
                config=TopicConfig(
                    num_partitions=len(partition_msg_count), replication_factor=1
                ),
            )
            sdf = app.dataframe(topic)
            sdf = sdf.apply(sum_value, stateful=True)
            return app, sdf, topic

        def validate_state():
            with state_manager_factory(
                group_id=consumer_group,
                state_dir=state_dir,
            ) as state_manager:
                state_manager.register_store(topic.name, store_name)
                for p_num, count in partition_msg_count.items():
                    state_manager.on_partition_assign(
                        topic=topic.name, partition=p_num, committed_offset=-1001
                    )
                    store = state_manager.get_store(
                        topic=topic.name, store_name=store_name
                    )
                    partition = store.partitions[p_num]
                    assert partition.get_changelog_offset() == count - 1
                    with partition.begin() as tx:
                        # All keys in state must be prefixed with the message key
                        prefix = f"key{p_num}".encode()
                        assert tx.get(sum_key, prefix=prefix) == count * msg_int_value

        # Produce messages to the topic and flush
        app, sdf, topic = get_app()
        with app.get_producer() as producer:
            for p_num, count in partition_msg_count.items():
                serialized = topic.serialize(
                    key=f"key{p_num}".encode(), value={"my_value": msg_int_value}
                )
                for _ in range(count):
                    producer.produce(
                        topic.name,
                        key=serialized.key,
                        value=serialized.value,
                        partition=p_num,
                    )

        # run app to populate state with data
        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        # validate and then delete the state
        assert processed_count == partition_msg_count
        validate_state()
        app.clear_state()

        # run the app again and validate the recovered state
        processed_count = {0: 0, 1: 0}
        app, sdf, topic = get_app()
        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        # no messages should have been processed outside of recovery loop
        assert processed_count == {0: 0, 1: 0}
        # State should be the same as before deletion
        validate_state()

    @pytest.mark.parametrize("processing_guarantee", ["at-least-once", "exactly-once"])
    def test_changelog_recovery_window_store(
        self,
        app_factory,
        executor,
        tmp_path,
        state_manager_factory,
        processing_guarantee,
    ):
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        topic_name = str(uuid.uuid4())
        store_name = "window"
        window_duration_ms = 5000
        window_step_ms = 2000
        msg_tick_ms = 1000
        msg_int_value = 10
        partition_timestamps = {
            0: list(range(1707260000000, 1707260004000, msg_tick_ms)),
            1: list(range(1707260000000, 1707260002000, msg_tick_ms)),
        }
        partition_windows = {
            p: [
                w
                for ts in ts_list
                for w in get_window_ranges(ts, window_duration_ms, window_step_ms)
            ]
            for p, ts_list in partition_timestamps.items()
        }

        # how many times window updates should occur (1:1 with changelog updates)
        expected_window_updates = {0: {}, 1: {}}
        # expired windows should have no values (changelog updates per tx == num_exp_windows + 1)
        expected_expired_windows = {0: set(), 1: set()}

        for p, windows in partition_windows.items():
            latest_timestamp = partition_timestamps[p][-1]
            for w in windows:
                if latest_timestamp >= w[1]:
                    expected_expired_windows[p].add(w)
                expected_window_updates[p][w] = (
                    expected_window_updates[p].setdefault(w, 0) + 1
                )

        processed_count = {0: 0, 1: 0}
        partition_msg_count = {
            p: len(partition_timestamps[p]) for p in partition_timestamps
        }

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            processed_count[partition] += 1
            if processed_count == partition_msg_count:
                done.set_result(True)

        def get_app():
            app = app_factory(
                commit_interval=0,  # Commit every processed message
                auto_offset_reset="earliest",
                use_changelog_topics=True,
                consumer_group=consumer_group,
                on_message_processed=on_message_processed,
                state_dir=state_dir,
                processing_guarantee=processing_guarantee,
            )
            topic = app.topic(
                topic_name,
                config=TopicConfig(
                    num_partitions=len(partition_msg_count), replication_factor=1
                ),
            )
            sdf = app.dataframe(topic)
            sdf = sdf.apply(lambda row: row["my_value"])
            sdf = (
                sdf.hopping_window(
                    duration_ms=window_duration_ms,
                    step_ms=window_step_ms,
                    name=store_name,
                )
                .sum()
                .final()
            )
            sdf = sdf.apply(
                lambda value: {
                    "sum": value["value"],
                    "window": (value["start"], value["end"]),
                }
            )
            return app, sdf, topic

        def validate_state():
            with state_manager_factory(
                group_id=consumer_group, state_dir=state_dir
            ) as state_manager:
                state_manager.register_windowed_store(topic.name, store_name)
                for p_num, windows in expected_window_updates.items():
                    state_manager.on_partition_assign(
                        topic=topic.name, partition=p_num, committed_offset=-1001
                    )
                    store = state_manager.get_store(
                        topic=topic.name, store_name=store_name
                    )

                    # in this test, each expiration check only deletes one window,
                    # simplifying the offset counting.
                    expected_offset = (
                        sum(expected_window_updates[p_num].values())
                        + 2 * len(expected_expired_windows[p_num])
                        - 1
                    )
                    if processing_guarantee == "exactly-once":
                        # In this test, we commit after each message is processed, so
                        # must add PMC-1 to our offset calculation since each kafka
                        # to account for transaction commit markers (except last one)
                        expected_offset += partition_msg_count[p_num] - 1
                    assert (
                        expected_offset
                        == store.partitions[p_num].get_changelog_offset()
                    )

                    partition = store.partitions[p_num]

                    with partition.begin() as tx:
                        prefix = f"key{p_num}".encode()
                        for window, count in windows.items():
                            expected = count
                            if window in expected_expired_windows[p_num]:
                                expected = None
                            else:
                                # each message value was 10
                                expected *= msg_int_value
                            assert tx.get_window(*window, prefix=prefix) == expected

        app, sdf, topic = get_app()
        # Produce messages to the topic and flush
        with app.get_producer() as producer:
            for p_num, timestamps in partition_timestamps.items():
                serialized = topic.serialize(
                    key=f"key{p_num}".encode(), value={"my_value": msg_int_value}
                )
                data = {
                    "key": serialized.key,
                    "value": serialized.value,
                    "partition": p_num,
                }
                for ts in timestamps:
                    data["timestamp"] = ts
                    producer.produce(topic.name, **data)

        # run app to populate state
        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        # validate and then delete the state
        assert processed_count == partition_msg_count
        validate_state()

        # run the app again and validate the recovered state
        processed_count = {0: 0, 1: 0}
        app, sdf, topic = get_app()
        app.clear_state()
        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        # no messages should have been processed outside of recovery loop
        assert processed_count == {0: 0, 1: 0}
        # State should be the same as before deletion
        validate_state()

    @pytest.mark.parametrize("processing_guarantee", ["at-least-once", "exactly-once"])
    def test_changelog_recovery_consistent_after_failed_commit(
        self,
        app_factory,
        executor,
        tmp_path,
        state_manager_factory,
        consumer_factory,
        processing_guarantee,
    ):
        """
        Scenario: application processes messages and successfully produces changelog
        messages but fails to commit the topic offsets.

        We expect that the app will be recovered to a consistent state and changes
        for the yet uncommitted messages will not be applied.
        """
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        topic_name = str(uuid.uuid4())
        store_name = "default"

        if processing_guarantee == "exactly-once":
            commit_patch = patch.object(
                RowProducer, "commit_transaction", side_effect=ValueError("Fail")
            )
        else:
            commit_patch = patch.object(
                RowConsumer, "commit", side_effect=ValueError("Fail")
            )

        # Messages to be processed successfully
        succeeded_messages = [
            ("key1", "1"),
            ("key2", "2"),
            ("key3", "3"),
        ]
        # Messages to fail
        failed_messages = [
            ("key1", "4"),
            ("key2", "5"),
            ("key3", "6"),
        ]
        # Ensure the same number of messages in both sets to simplify testing
        assert len(failed_messages) == len(succeeded_messages)
        total_count = len(succeeded_messages)
        processed_count = 0

        def on_message_processed(topic_, partition, offset):
            nonlocal processed_count
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            processed_count += 1
            if processed_count == total_count:
                done.set_result(True)

        def get_app():
            app = app_factory(
                commit_interval=999,  # Simulate a very long commit interval
                auto_offset_reset="earliest",
                use_changelog_topics=True,
                on_message_processed=on_message_processed,
                consumer_group=consumer_group,
                state_dir=state_dir,
                processing_guarantee=processing_guarantee,
            )
            topic = app.topic(topic_name)
            sdf = app.dataframe(topic)
            sdf = sdf.update(
                lambda value, state: state.set("latest", value["number"]), stateful=True
            )
            return app, sdf, topic

        def validate_state():
            with state_manager_factory(
                group_id=consumer_group,
                state_dir=state_dir,
            ) as state_manager, consumer_factory(
                consumer_group=consumer_group
            ) as consumer:
                committed_offset = consumer.committed(
                    [TopicPartition(topic=topic_name, partition=0)]
                )[0].offset
                state_manager.register_store(topic.name, store_name)
                partition = state_manager.on_partition_assign(
                    topic=topic.name, partition=0, committed_offset=committed_offset
                )[0]
                with partition.begin() as tx:
                    for key, value in succeeded_messages:
                        state = tx.as_state(prefix=key.encode())
                        assert state.get("latest") == value

        # Produce messages from the "succeeded" set
        app, sdf, topic = get_app()
        with app.get_producer() as producer:
            for key, value in succeeded_messages:
                serialized = topic.serialize(key=key.encode(), value={"number": value})
                producer.produce(topic.name, key=serialized.key, value=serialized.value)

        # Run the application to apply changes to state
        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run(sdf)
        assert processed_count == total_count
        # Validate the state
        validate_state()

        # Init application again
        processed_count = 0
        app, sdf, topic = get_app()

        # Produce messages from the "failed" set
        with app.get_producer() as producer:
            for key, value in failed_messages:
                serialized = topic.serialize(key=key.encode(), value={"number": value})
                producer.produce(topic.name, key=serialized.key, value=serialized.value)

        # Run the app second time and fail the consumer commit
        with commit_patch:
            done = Future()
            executor.submit(_stop_app_on_future, app, done, 10.0)
            with contextlib.suppress(PartitionAssignmentError):
                with pytest.raises(ValueError):
                    app.run(sdf)
        # state should remain the same
        validate_state()

        # Run the app again to recover the state
        app, sdf, topic = get_app()
        # Clear the state to recover from scratch
        app.clear_state()

        # Run app for the third time and fail on commit to prevent state changes
        with commit_patch:
            done = Future()
            executor.submit(_stop_app_on_future, app, done, 10.0)
            with contextlib.suppress(PartitionAssignmentError):
                with pytest.raises(ValueError):
                    app.run(sdf)
        # The app should be recovered
        validate_state()


class TestApplicationSink:
    def test_run_with_sink_success(
        self,
        app_factory,
        executor,
    ):

        processed_count = 0
        total_messages = 3

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )
        sink = DummySink()

        topic = app.topic(
            str(uuid.uuid4()),
            value_deserializer="str",
            config=TopicConfig(num_partitions=3, replication_factor=1),
        )
        sdf = app.dataframe(topic)
        sdf.sink(sink)

        key, value, timestamp_ms = b"key", "value", 1000
        headers = [("key", b"value")]

        # Produce messages to different topic partitions and flush
        with app.get_producer() as producer:
            for i in range(total_messages):
                producer.produce(
                    topic=topic.name,
                    partition=i,
                    key=key,
                    value=value,
                    timestamp=timestamp_ms,
                    headers=headers,
                )

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 15.0)
        app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Ensure all messages were flushed to the sink
        assert len(sink.results) == 3
        for item in sink.results:
            assert item.key == key
            assert item.value == value
            assert item.timestamp == timestamp_ms
            assert item.headers == headers

        # Ensure that the offsets are committed
        with app.get_consumer() as consumer:
            committed0, committed1, committed2 = consumer.committed(
                [
                    TopicPartition(topic=topic.name, partition=0),
                    TopicPartition(topic=topic.name, partition=1),
                    TopicPartition(topic=topic.name, partition=2),
                ]
            )
        assert committed0.offset == 1
        assert committed1.offset == 1
        assert committed2.offset == 1

    def test_run_with_sink_backpressure(
        self,
        app_factory,
        executor,
    ):
        """
        Test that backpressure is handled correctly by the app
        """

        total_messages = 10
        topic_name = str(uuid.uuid4())
        partition = 0

        class _BackpressureSink(DummySink):
            _backpressured = False

            def write(self, batch: SinkBatch):
                # Backpressure sink once here to ensure the offset rewind works
                if not self._backpressured:
                    self._backpressured = True
                    raise SinkBackpressureError(
                        topic=topic_name, partition=partition, retry_after=1
                    )
                return super().write(batch=batch)

        app = app_factory(
            auto_offset_reset="earliest",
            commit_interval=1.0,  # Commit every second
        )
        sink = _BackpressureSink()

        topic = app.topic(
            topic_name,
            value_deserializer="str",
        )
        sdf = app.dataframe(topic)
        sdf.sink(sink)

        key, value, timestamp_ms = b"key", "value", 1000
        headers = [("key", b"value")]

        # Produce messages to different topic partitions and flush
        with app.get_producer() as producer:
            for _ in range(total_messages):
                producer.produce(
                    topic=topic.name,
                    key=key,
                    value=value,
                    timestamp=timestamp_ms,
                    headers=headers,
                )

        executor.submit(_stop_app_on_timeout, app, 15.0)
        app.run(sdf)

        # Ensure all messages were flushed to the sink
        assert len(sink.results) == total_messages
        for item in sink.results:
            assert item.key == key
            assert item.value == value
            assert item.timestamp == timestamp_ms
            assert item.headers == headers

        # Ensure that the offsets are committed
        with app.get_consumer() as consumer:
            committed, *_ = consumer.committed(
                [TopicPartition(topic=topic.name, partition=0)]
            )
        assert committed.offset == total_messages

    def test_run_with_sink_branches_success(
        self,
        app_factory,
        executor,
    ):

        processed_count = 0
        total_messages = 3

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )
        sink = DummySink()

        topic = app.topic(
            str(uuid.uuid4()),
            value_deserializer="str",
            config=TopicConfig(num_partitions=3, replication_factor=1),
        )
        sdf = app.dataframe(topic)
        sdf = sdf.apply(lambda x: x + "_branch")
        sdf.apply(lambda x: x + "0").sink(sink)
        sdf.apply(lambda x: x + "1").sink(sink)
        sdf = sdf.apply(lambda x: x + "2")
        sdf.sink(sink)

        key, value, timestamp_ms = b"key", "value", 1000
        headers = [("key", b"value")]

        # Produce messages to different topic partitions and flush
        with app.get_producer() as producer:
            for i in range(total_messages):
                producer.produce(
                    topic=topic.name,
                    partition=i,
                    key=key,
                    value=value,
                    timestamp=timestamp_ms,
                    headers=headers,
                )

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 15.0)
        app.run(sdf)

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Ensure all messages were flushed to the sink
        assert len(sink.results) == 9
        for i in range(3):
            assert (
                len([r for r in sink.results if f"_branch{i}" in r.value])
                == total_messages
            )
        for item in sink.results:
            assert item.key == key
            assert value in item.value
            assert item.timestamp == timestamp_ms
            assert item.headers == headers

        # Ensure that the offsets are committed
        with app.get_consumer() as consumer:
            committed0, committed1, committed2 = consumer.committed(
                [
                    TopicPartition(topic=topic.name, partition=0),
                    TopicPartition(topic=topic.name, partition=1),
                    TopicPartition(topic=topic.name, partition=2),
                ]
            )
        assert committed0.offset == 1
        assert committed1.offset == 1
        assert committed2.offset == 1


class TestApplicationSource:

    MESSAGES_COUNT = 3

    def wait_finished(self, app, event, timeout=15.0):
        try:
            event.wait(timeout)
        finally:
            app.stop()

    def test_run_with_source_success(
        self,
        app_factory,
        executor,
    ):

        done = Future()
        processed_count = 0

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == self.MESSAGES_COUNT:
                done.set_result(True)

        app = app_factory(
            auto_offset_reset="earliest", on_message_processed=on_message_processed
        )
        source = DummySource(values=range(self.MESSAGES_COUNT))
        sdf = app.dataframe(source=source)

        executor.submit(_stop_app_on_future, app, done, 10.0)

        values = []
        sdf = sdf.apply(lambda value: values.append(value))
        app.run(sdf)

        assert values == [0, 1, 2]

    def test_run_source_only(self, app_factory, executor):

        done = multiprocessing.Event()

        topic_name = str(uuid.uuid4())
        app = app_factory(
            auto_offset_reset="earliest",
        )

        source = DummySource(values=range(self.MESSAGES_COUNT), finished=done)
        app.add_source(source, topic=app.topic(topic_name))

        executor.submit(self.wait_finished, app, done, 15.0)

        app._run()

        results = []
        with app.get_consumer() as consumer:
            consumer.subscribe(topics=[topic_name])

            for _ in range(self.MESSAGES_COUNT):
                msg = consumer.poll()
                results.append(msg.value())

        assert results == [b"0", b"1", b"2"]

    @pytest.mark.parametrize(
        "raise_is,exitcode", [("run", 0), ("cleanup", 0), ("stop", -9)]
    )
    @pytest.mark.parametrize("pickleable", [True, False])
    def test_source_with_error(
        self, app_factory, executor, raise_is, exitcode, pickleable
    ):
        done = multiprocessing.Event()

        app = app_factory(
            auto_offset_reset="earliest",
        )

        source = DummySource(
            values=range(self.MESSAGES_COUNT),
            finished=done,
            error_in=raise_is,
            pickeable_error=pickleable,
        )
        sdf = app.dataframe(source=source)

        executor.submit(self.wait_finished, app, done, 15.0)

        # The app stops on source error
        try:
            with pytest.raises(SourceException) as exc:
                app.run(sdf)
        finally:
            # shutdown the thread waiting for exit
            done.set()

        assert exc.value.exitcode == exitcode
        assert exc.value.__cause__
        if pickleable:
            assert isinstance(exc.value.__cause__, ValueError)
        else:
            assert isinstance(exc.value.__cause__, RuntimeError)
        assert str(exc.value.__cause__) == f"test {raise_is} error"


class TestApplicationMultipleSdf:
    def test_multiple_sdfs(
        self,
        app_factory,
        row_consumer_factory,
        executor,
    ):
        """
        Test that StreamingDataFrame processes 3 messages from Kafka by having the
        app produce the consumed messages verbatim to a new topic, and of course
        committing the respective offsets after handling each message.
        """

        def on_message_processed(topic_, partition, offset):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )

        partition_num = 0
        topic_a = app.topic(
            str(uuid.uuid4()),
            value_deserializer=JSONDeserializer(),
        )
        topic_b = app.topic(
            str(uuid.uuid4()),
            value_deserializer=JSONDeserializer(),
        )
        input_topics = [topic_a, topic_b]
        topic_out = app.topic(
            str(uuid.uuid4()),
            value_serializer=JSONSerializer(),
            value_deserializer=JSONDeserializer(),
        )
        sdf_a = app.dataframe(topic_a)
        sdf_a.to_topic(topic_out)

        sdf_b = app.dataframe(topic_b)
        sdf_b.to_topic(topic_out)

        processed_count = 0
        messages_per_topic = 3
        total_messages = messages_per_topic * len(input_topics)
        # Produce messages to the topic and flush
        timestamp_ms = int(time.time() / 1000)
        headers = [("header", b"value")]
        data = {
            "key": b"key",
            "value": b'"value"',
            "partition": partition_num,
            "timestamp": timestamp_ms,
            "headers": headers,
        }
        with app.get_producer() as producer:
            for topic in input_topics:
                for _ in range(messages_per_topic):
                    producer.produce(topic.name, **data)

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 15.0)
        app.run()

        # Check that all messages have been processed
        assert processed_count == total_messages

        # Ensure that the right offset is committed
        with row_consumer_factory(auto_offset_reset="latest") as row_consumer:
            committed = row_consumer.committed(
                [TopicPartition(topic.name, partition_num) for topic in input_topics]
            )
            for topic in committed:
                assert topic.offset == messages_per_topic

        # confirm messages actually ended up being produced by the app
        rows_out = []
        with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
            row_consumer.subscribe([topic_out])
            while len(rows_out) < total_messages:
                rows_out.append(row_consumer.poll_row(timeout=5))

        assert len(rows_out) == total_messages
        for row in rows_out:
            assert row.topic == topic_out.name
            assert row.key == data["key"]
            assert row.value == loads(data["value"].decode())
            assert row.timestamp == timestamp_ms
            assert row.headers == headers

    def test_group_by(
        self,
        app_factory,
        row_consumer_factory,
        executor,
    ):
        """
        Test that StreamingDataFrame processes 6 messages from Kafka and groups them
        by each record's specified column value.
        """

        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == expected_processed:
                done.set_result(True)

        processed_count = 0

        app = app_factory(
            auto_offset_reset="earliest",
            on_message_processed=on_message_processed,
        )
        input_topic_a = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )
        input_topic_b = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )
        input_topics = [input_topic_a, input_topic_b]
        output_topic_user = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )
        output_topic_account = app.topic(
            str(uuid.uuid4()),
            value_deserializer="json",
            value_serializer="json",
        )

        timestamp = 1000
        user_id = "abc123"
        account_id = "def456"
        value_in = {"user": user_id, "account": account_id}
        # expected_processed = 1 (input msg per SDF) * 3 (2 groupbys, each reprocesses input) * 2 SDFs
        expected_processed = 6
        expected_output_topic_count = 2

        sdf_a = app.dataframe(topic=input_topic_a)
        sdf_a_user = sdf_a.group_by("user")
        sdf_a_user["groupby_timestamp"] = sdf_a_user.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf_a_user.to_topic(output_topic_user)

        sdf_a_account = sdf_a.group_by("account")
        sdf_a_account["groupby_timestamp"] = sdf_a_account.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf_a_account.to_topic(output_topic_account)

        sdf_b = app.dataframe(topic=input_topic_b)
        sdf_b_user = sdf_b.group_by("user")
        sdf_b_user["groupby_timestamp"] = sdf_b_user.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf_b_user.to_topic(output_topic_user)

        sdf_b_account = sdf_b.group_by("account")
        sdf_b_account["groupby_timestamp"] = sdf_b_account.apply(
            lambda value, key, timestamp_, headers: timestamp_, metadata=True
        )
        sdf_b_account.to_topic(output_topic_account)

        with app.get_producer() as producer:
            for topic in input_topics:
                msg = topic.serialize(
                    key="some_key", value=value_in, timestamp_ms=timestamp
                )
                producer.produce(
                    topic.name, key=msg.key, value=msg.value, timestamp=msg.timestamp
                )

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run()

        # Check that all messages have been processed
        assert processed_count == expected_processed

        # Consume the message from the output topic
        for key, output_topic in [
            (user_id, output_topic_user),
            (account_id, output_topic_account),
        ]:
            rows = []
            with row_consumer_factory(auto_offset_reset="earliest") as row_consumer:
                row_consumer.subscribe([output_topic])
                while row := row_consumer.poll_row(timeout=5):
                    rows.append(row)

            assert len(rows) == expected_output_topic_count
            for row in rows:
                # Check that "user_id" is now used as a message key
                assert row.key.decode() == key
                # Check that message timestamp of the repartitioned message is the same
                # as original one
                assert row.value == {
                    **value_in,
                    "groupby_timestamp": timestamp,
                }

    def test_stateful(
        self,
        app_factory,
        executor,
        state_manager_factory,
        tmp_path,
    ):
        """
        Test that StreamingDataFrame processes 3 messages from Kafka and updates
        the counter in the state store
        """

        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        processed_count = 0

        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        partition_num = 0
        app = app_factory(
            commit_interval=0,
            consumer_group=consumer_group,
            auto_offset_reset="earliest",
            state_dir=state_dir,
            on_message_processed=on_message_processed,
            use_changelog_topics=True,
        )
        input_topic_a = app.topic(
            str(uuid.uuid4()), value_deserializer=JSONDeserializer()
        )
        input_topic_b = app.topic(
            str(uuid.uuid4()), value_deserializer=JSONDeserializer()
        )
        input_topics = [input_topic_a, input_topic_b]
        messages_per_topic = 3
        total_messages = messages_per_topic * len(input_topics)

        # Define a function that counts incoming Rows using state
        def count(_, state: State):
            total = state.get("total", 0)
            total += 1
            state.set("total", total)

        sdf_a = app.dataframe(input_topic_a)
        sdf_a.update(count, stateful=True)

        sdf_b = app.dataframe(input_topic_b)
        sdf_b.update(count, stateful=True)

        # Produce messages to the topic and flush
        message_key = b"key"
        data = {
            "key": message_key,
            "value": dumps({"key": "value"}),
            "partition": partition_num,
        }
        with app.get_producer() as producer:
            for topic in input_topics:
                for _ in range(messages_per_topic):
                    producer.produce(topic.name, **data)

        done = Future()

        # Stop app when the future is resolved
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run()
        assert processed_count == total_messages

        # Check that the values are actually in the DB
        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )
        for topic in input_topics:
            state_manager.register_store(topic.name, "default")
            state_manager.on_partition_assign(
                topic=topic.name, partition=partition_num, committed_offset=-1001
            )
            store = state_manager.get_store(topic=topic.name, store_name="default")
            with store.start_partition_transaction(partition=partition_num) as tx:
                # All keys in state must be prefixed with the message key
                assert tx.get("total", prefix=message_key) == messages_per_topic

    def test_changelog_recovery(
        self,
        app_factory,
        executor,
        tmp_path,
        state_manager_factory,
    ):
        def on_message_processed(*_):
            # Set the callback to track total messages processed
            # The callback is not triggered if processing fails
            nonlocal processed_count

            processed_count += 1
            # Stop processing after consuming all the messages
            if processed_count == total_messages:
                done.set_result(True)

        processed_count = 0
        input_topic_a_name = str(uuid.uuid4())
        input_topic_b_name = str(uuid.uuid4())
        consumer_group = str(uuid.uuid4())
        state_dir = (tmp_path / "state").absolute()
        partition_num = 0
        messages_per_topic = 3
        total_messages = 2 * messages_per_topic  # 2 topics
        message_key = b"key"
        data = {
            "key": message_key,
            "value": dumps({"key": "value"}),
            "partition": partition_num,
        }

        def count(_, state: State):
            total = state.get("total", 0)
            total += 1
            state.set("total", total)

        def produce_messages(app, topics):
            with app.get_producer() as producer:
                for topic in topics:
                    for _ in range(messages_per_topic):
                        producer.produce(topic.name, **data)

        def get_app():
            app = app_factory(
                commit_interval=0,  # Commit every processed message
                use_changelog_topics=True,
                consumer_group=consumer_group,
                auto_offset_reset="earliest",
                state_dir=state_dir,
                on_message_processed=on_message_processed,
            )

            input_topic_a = app.topic(
                input_topic_a_name, value_deserializer=JSONDeserializer()
            )
            input_topic_b = app.topic(
                input_topic_b_name, value_deserializer=JSONDeserializer()
            )
            input_topics = [input_topic_a, input_topic_b]

            sdf_a = app.dataframe(input_topic_a)
            sdf_a.update(count, stateful=True)

            sdf_b = app.dataframe(input_topic_b)
            sdf_b.update(count, stateful=True)

            return app, input_topics

        # produce messages, then run app until all are processed else timeout
        app, input_topics = get_app()
        produce_messages(app, input_topics)

        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run()
        assert processed_count == total_messages

        # Clear state, repeat the same produce/run process
        # Should result in 2x the original expected count
        processed_count = 0
        app, input_topics = get_app()
        app.clear_state()
        produce_messages(app, input_topics)

        done = Future()
        executor.submit(_stop_app_on_future, app, done, 10.0)
        app.run()
        assert processed_count == total_messages

        state_manager = state_manager_factory(
            group_id=consumer_group, state_dir=state_dir
        )

        for topic in input_topics:
            state_manager.register_store(topic.name, "default")
            state_manager.on_partition_assign(
                topic=topic.name, partition=partition_num, committed_offset=-1001
            )
            store = state_manager.get_store(topic=topic.name, store_name="default")
            with store.start_partition_transaction(partition=partition_num) as tx:
                # All keys in state must be prefixed with the message key
                assert tx.get("total", prefix=message_key) == messages_per_topic * 2
