"""Redis connection settings that must survive dependency upgrades."""

import unittest
from unittest import mock

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

    def _pool_kwargs(self):
        import redis

        with mock.patch.object(redis, "ConnectionPool") as pool_cls, mock.patch.object(
            redis, "Redis"
        ) as redis_cls:
            redis_cls.return_value.ping.return_value = True
            client = redis_client.get_redis()
        self.assertIs(client, redis_cls.return_value)
        return pool_cls.call_args.kwargs

    def test_pool_pins_resp2_for_older_servers(self):
        self.assertEqual(self._pool_kwargs()["protocol"], 2)

    def test_pool_does_not_retry_the_connect(self):
        # An unreachable Redis must fall back to local mode after one attempt.
        retry = self._pool_kwargs()["retry"]
        self.assertEqual(retry._retries, 0)

    def test_unreachable_redis_falls_back_to_none(self):
        import redis

        with mock.patch.object(redis, "Redis") as redis_cls:
            redis_cls.return_value.ping.side_effect = redis.ConnectionError("refused")
            with self.assertLogs(redis_client.logger, level="WARNING"):
                self.assertIsNone(redis_client.get_redis())
        self.assertFalse(redis_client._REDIS_AVAILABLE)


if __name__ == "__main__":
    unittest.main()
