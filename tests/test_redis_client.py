"""Redis connection settings that must survive dependency upgrades."""

import socket
import unittest
from unittest import mock

import redis

from core import redis_client


class RedisPoolSettingsTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            redis_client._REDIS_CLIENT,
            redis_client._REDIS_AVAILABLE,
            redis_client._LAST_CONNECT_TRY,
        )
        redis_client._REDIS_CLIENT = None
        redis_client._REDIS_AVAILABLE = None
        redis_client._LAST_CONNECT_TRY = 0

    def tearDown(self):
        (
            redis_client._REDIS_CLIENT,
            redis_client._REDIS_AVAILABLE,
            redis_client._LAST_CONNECT_TRY,
        ) = self._saved

    def _connection(self):
        """Build the real pool get_redis() builds and return one (unconnected) connection."""
        with mock.patch.object(redis, "Redis") as redis_cls:
            redis_cls.return_value.ping.return_value = True
            redis_client.get_redis()
        pool = redis_cls.call_args.kwargs["connection_pool"]
        self.assertIsInstance(pool, redis.ConnectionPool)
        return pool.make_connection()

    def test_pool_pins_resp2_for_older_servers(self):
        self.assertEqual(self._connection().protocol, 2)

    def test_timeouts_are_retried_once(self):
        retry = self._connection().retry
        self.assertEqual(retry.get_retries(), 1)
        self.assertIn(redis.exceptions.TimeoutError, retry._supported_errors)
        self.assertIn(socket.timeout, retry._supported_errors)

    def test_connection_errors_are_not_retried(self):
        # An unreachable Redis must fall back to local mode after one connect attempt.
        retry = self._connection().retry
        attempts = []

        def refuse():
            attempts.append(1)
            raise redis.exceptions.ConnectionError("refused")

        with self.assertRaises(redis.exceptions.ConnectionError):
            retry.call_with_retry(refuse, lambda error: None)
        self.assertEqual(len(attempts), 1)

    def test_unreachable_redis_falls_back_to_none(self):
        with mock.patch.object(redis, "Redis") as redis_cls:
            redis_cls.return_value.ping.side_effect = redis.ConnectionError("refused")
            with self.assertLogs(redis_client.logger, level="WARNING"):
                self.assertIsNone(redis_client.get_redis())
        self.assertFalse(redis_client._REDIS_AVAILABLE)


if __name__ == "__main__":
    unittest.main()
