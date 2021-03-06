from __future__ import absolute_import

import calendar
from datetime import datetime, timedelta
import json
import pytz
import requests
import six

from django.conf import settings

from sentry.models import GroupHash, GroupRelease, Release
from sentry.tsdb.base import TSDBModel
from sentry.tsdb.snuba import SnubaTSDB
from sentry.testutils import TestCase
from sentry.utils.dates import to_timestamp


def timestamp(d):
    t = int(to_timestamp(d))
    return t - (t % 3600)


def has_shape(data, shape, allow_empty=False):
    """
    Determine if a data object has the provided shape

    At any level, the object in `data` and in `shape` must have the same type.
    A dict is the same shape if all its keys and values have the same shape as the
    key/value in `shape`. The number of keys/values is not relevant.
    A list is the same shape if all its items have the same shape as the value
    in `shape`
    A tuple is the same shape if it has the same length as `shape` and all the
    values have the same shape as the corresponding value in `shape`
    Any other object simply has to have the same type.
    If `allow_empty` is set, lists and dicts in `data` will pass even if they are empty.
    """
    if not isinstance(data, type(shape)):
        return False
    if isinstance(data, dict):
        return (allow_empty or len(data) > 0) and\
            all(has_shape(k, shape.keys()[0]) for k in data.keys()) and\
            all(has_shape(v, shape.values()[0]) for v in data.values())
    elif isinstance(data, list):
        return (allow_empty or len(data) > 0) and\
            all(has_shape(v, shape[0]) for v in data)
    elif isinstance(data, tuple):
        return len(data) == len(shape) and all(
            has_shape(data[i], shape[i]) for i in range(len(data)))
    else:
        return True


class SnubaTSDBTest(TestCase):
    def setUp(self):
        assert requests.post(settings.SENTRY_SNUBA + '/tests/drop').status_code == 200

        self.db = SnubaTSDB()
        self.now = datetime.utcnow().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=pytz.UTC
        )

        self.proj1 = self.create_project()
        self.proj1env1 = self.create_environment(project=self.proj1, name='test')
        self.proj1env2 = self.create_environment(project=self.proj1, name='dev')
        self.proj1defaultenv = self.create_environment(project=self.proj1, name='')

        self.proj1group1 = self.create_group(self.proj1)
        self.proj1group2 = self.create_group(self.proj1)

        hash1 = '1' * 32
        hash2 = '2' * 32
        GroupHash.objects.create(project=self.proj1, group=self.proj1group1, hash=hash1)
        GroupHash.objects.create(project=self.proj1, group=self.proj1group2, hash=hash2)

        self.release1 = Release.objects.create(
            organization_id=self.organization.id,
            version='1' * 10,
            date_added=self.now,
        )
        self.release1.add_project(self.proj1)
        self.release2 = Release.objects.create(
            organization_id=self.organization.id,
            version='2' * 10,
            date_added=self.now,
        )
        self.release2.add_project(self.proj1)

        self.group1release1 = GroupRelease.objects.create(
            project_id=self.proj1.id,
            group_id=self.proj1group1.id,
            release_id=self.release1.id
        )
        self.group1release2 = GroupRelease.objects.create(
            project_id=self.proj1.id,
            group_id=self.proj1group1.id,
            release_id=self.release2.id
        )
        self.group2release1 = GroupRelease.objects.create(
            project_id=self.proj1.id,
            group_id=self.proj1group2.id,
            release_id=self.release1.id
        )

        data = json.dumps([{
            'event_id': (six.text_type(r) * 32)[:32],
            'primary_hash': [hash1, hash2][(r // 600) % 2],  # Switch every 10 mins
            'group_id': int([hash1, hash2][(r // 600) % 2][:16], 16),
            'project_id': self.proj1.id,
            'message': 'message 1',
            'platform': 'python',
            'datetime': (self.now + timedelta(seconds=r)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            'data': {
                'received': calendar.timegm(self.now.timetuple()) + r,
                'tags': {
                    'foo': 'bar',
                    'baz': 'quux',
                    # Switch every 2 hours
                    'environment': [self.proj1env1.name, None][(r // 7200) % 2],
                    'sentry:user': u'id:user{}'.format(r // 3300),
                    'sentry:release': six.text_type(r // 3600) * 10,  # 1 per hour
                },
                'sentry.interfaces.User': {
                    # change every 55 min so some hours have 1 user, some have 2
                    'id': u"user{}".format(r // 3300),
                    'email': u"user{}@sentry.io".format(r)
                }
            },
        } for r in range(0, 14400, 600)])  # Every 10 min for 4 hours

        assert requests.post(settings.SENTRY_SNUBA + '/tests/insert', data=data).status_code == 200

        # snuba trims query windows based on first_seen/last_seen, so these need to be correct-ish
        self.proj1group1.first_seen = self.now
        self.proj1group1.last_seen = self.now + timedelta(seconds=14400)
        self.proj1group1.save()
        self.proj1group2.first_seen = self.now
        self.proj1group2.last_seen = self.now + timedelta(seconds=14400)
        self.proj1group2.save()

    def test_range_groups(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_range(
            TSDBModel.group,
            [self.proj1group1.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.proj1group1.id: [
                (timestamp(dts[0]), 3),
                (timestamp(dts[1]), 3),
                (timestamp(dts[2]), 3),
                (timestamp(dts[3]), 3),
            ],
        }

        # Multiple groups
        assert self.db.get_range(
            TSDBModel.group,
            [self.proj1group1.id, self.proj1group2.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.proj1group1.id: [
                (timestamp(dts[0]), 3),
                (timestamp(dts[1]), 3),
                (timestamp(dts[2]), 3),
                (timestamp(dts[3]), 3),
            ],
            self.proj1group2.id: [
                (timestamp(dts[0]), 3),
                (timestamp(dts[1]), 3),
                (timestamp(dts[2]), 3),
                (timestamp(dts[3]), 3),
            ],
        }

    def test_range_releases(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_range(
            TSDBModel.release,
            [self.release1.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.release1.id: [
                (timestamp(dts[0]), 0),
                (timestamp(dts[1]), 6),
                (timestamp(dts[2]), 0),
                (timestamp(dts[3]), 0),
            ]
        }

    def test_range_project(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 6),
                (timestamp(dts[1]), 6),
                (timestamp(dts[2]), 6),
                (timestamp(dts[3]), 6),
            ]
        }

    def test_range_environment_filter(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=3600,
            environment_id=self.proj1env1.id
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 6),
                (timestamp(dts[1]), 6),
                (timestamp(dts[2]), 0),
                (timestamp(dts[3]), 0),
            ]
        }

        # No events submitted for env2
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=3600,
            environment_id=self.proj1env2.id
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 0),
                (timestamp(dts[1]), 0),
                (timestamp(dts[2]), 0),
                (timestamp(dts[3]), 0),
            ]
        }

        # Events submitted with no environment should match default environment
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=3600,
            environment_id=self.proj1defaultenv.id
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 0),
                (timestamp(dts[1]), 0),
                (timestamp(dts[2]), 6),
                (timestamp(dts[3]), 6),
            ]
        }

    def test_range_rollups(self):
        # Daily
        daystart = self.now.replace(hour=0)  # day buckets start on day boundaries
        dts = [daystart + timedelta(days=i) for i in range(2)]
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=86400
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 24),
                (timestamp(dts[1]), 0)
            ]
        }

        # Minutely
        dts = [self.now + timedelta(minutes=i) for i in range(120)]
        # Expect every 10th minute to have a 1, else 0
        expected = [(to_timestamp(d), int(i % 10 == 0)) for i, d in enumerate(dts)]
        assert self.db.get_range(
            TSDBModel.project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=60
        ) == {
            self.proj1.id: expected
        }

    def test_distinct_counts_series_users(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_distinct_counts_series(
            TSDBModel.users_affected_by_group,
            [self.proj1group1.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.proj1group1.id: [
                (timestamp(dts[0]), 1),
                (timestamp(dts[1]), 1),
                (timestamp(dts[2]), 1),
                (timestamp(dts[3]), 2),
            ],
        }

        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_distinct_counts_series(
            TSDBModel.users_affected_by_project,
            [self.proj1.id],
            dts[0], dts[-1],
            rollup=3600
        ) == {
            self.proj1.id: [
                (timestamp(dts[0]), 1),
                (timestamp(dts[1]), 2),
                (timestamp(dts[2]), 2),
                (timestamp(dts[3]), 2),
            ],
        }

    def get_distinct_counts_totals_users(self):
        assert self.db.get_distinct_counts_totals(
            TSDBModel.users_affected_by_group,
            [self.proj1group1.id],
            self.now,
            self.now + timedelta(hours=4),
            rollup=3600
        ) == {
            self.proj1group1.id: 2,  # 2 unique users overall
        }

        assert self.db.get_distinct_counts_totals(
            TSDBModel.users_affected_by_group,
            [self.proj1group1.id],
            self.now,
            self.now,
            rollup=3600
        ) == {
            self.proj1group1.id: 1,  # Only 1 unique user in the first hour
        }

        assert self.db.get_distinct_counts_totals(
            TSDBModel.users_affected_by_project,
            [self.proj1.id],
            self.now,
            self.now + timedelta(hours=4),
            rollup=3600
        ) == {
            self.proj1.id: 2,
        }

    def test_most_frequent(self):
        assert self.db.get_most_frequent(
            TSDBModel.frequent_issues_by_project,
            [self.proj1.id],
            self.now,
            self.now + timedelta(hours=4),
            rollup=3600,
        ) == {
            self.proj1.id: [
                (self.proj1group1.id, 2.0),
                (self.proj1group2.id, 1.0),
            ],
        }

    def test_frequency_series(self):
        dts = [self.now + timedelta(hours=i) for i in range(4)]
        assert self.db.get_frequency_series(
            TSDBModel.frequent_releases_by_group,
            {
                self.proj1group1.id: (self.group1release1.id, self.group1release2.id, ),
                self.proj1group2.id: (self.group2release1.id, )
            },
            dts[0], dts[-1],
            rollup=3600,
        ) == {
            self.proj1group1.id: [
                (timestamp(dts[0]), {
                    self.group1release1.id: 0,
                    self.group1release2.id: 0,
                }),
                (timestamp(dts[1]), {
                    self.group1release1.id: 3,
                    self.group1release2.id: 0,
                }),
                (timestamp(dts[2]), {
                    self.group1release1.id: 0,
                    self.group1release2.id: 3,
                }),
                (timestamp(dts[3]), {
                    self.group1release1.id: 0,
                    self.group1release2.id: 0,
                }),
            ],
            self.proj1group2.id: [
                (timestamp(dts[0]), {
                    self.group2release1.id: 0,
                }),
                (timestamp(dts[1]), {
                    self.group2release1.id: 3,
                }),
                (timestamp(dts[2]), {
                    self.group2release1.id: 0,
                }),
                (timestamp(dts[3]), {
                    self.group2release1.id: 0,
                }),
            ],
        }

    def test_result_shape(self):
        """
        Tests that the results from the different TSDB methods have the
        expected format.
        """
        project_id = self.proj1.id
        dts = [self.now + timedelta(hours=i) for i in range(4)]

        results = self.db.get_most_frequent(TSDBModel.frequent_issues_by_project,
                                            [project_id], dts[0], dts[0])
        assert has_shape(results, {1: [(1, 1.0)]})

        results = self.db.get_most_frequent_series(TSDBModel.frequent_issues_by_project,
                                                   [project_id], dts[0], dts[0])
        assert has_shape(results, {1: [(1, {1: 1.0})]})

        items = {
            # {project_id: (issue_id, issue_id, ...)}
            project_id: (self.proj1group1.id, self.proj1group2.id)
        }
        results = self.db.get_frequency_series(TSDBModel.frequent_issues_by_project,
                                               items, dts[0], dts[-1])
        assert has_shape(results, {1: [(1, {1: 1})]})

        results = self.db.get_frequency_totals(TSDBModel.frequent_issues_by_project,
                                               items, dts[0], dts[-1])
        assert has_shape(results, {1: {1: 1}})

        results = self.db.get_range(TSDBModel.project, [project_id], dts[0], dts[-1])
        assert has_shape(results, {1: [(1, 1)]})

        results = self.db.get_distinct_counts_series(TSDBModel.users_affected_by_project,
                                                     [project_id], dts[0], dts[-1])
        assert has_shape(results, {1: [(1, 1)]})

        results = self.db.get_distinct_counts_totals(TSDBModel.users_affected_by_project,
                                                     [project_id], dts[0], dts[-1])
        assert has_shape(results, {1: 1})

        results = self.db.get_distinct_counts_union(TSDBModel.users_affected_by_project,
                                                    [project_id], dts[0], dts[-1])
        assert has_shape(results, 1)
