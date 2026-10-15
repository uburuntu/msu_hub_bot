"""Dashboard totals preserve isolated and restarted counters without double counting."""

import json
from pathlib import Path

import pytest

from test_link_dashboard import database as database


ROOT = Path(__file__).resolve().parents[1] / "tools/observability/dashboards"


def panel_query(dashboard, panel):
    definition = json.loads((ROOT / (dashboard + ".json")).read_text())
    return definition["definition"]["spec"]["panels"][panel]["spec"]["queries"][0]["spec"]["plugin"]["spec"]["query"]


@pytest.mark.parametrize(
    ("dashboard", "panel", "metric", "attributes", "measure", "expected"),
    [
        (
            "jobs",
            "outcomes",
            "bot.feature_jobs.transitions",
            {"feature": "chess", "job.kind": "finish", "state": "done"},
            "transitions",
            17,
        ),
        ("pulse", "telemetry-loss", "bot.telemetry.dropped_logs", {}, "dropped", 17),
        ("pulse", "poll-outcomes", "bot.poll.requests", {}, "requests_per_minute", 10),
    ],
)
def test_operation_counters_isolate_processes_epochs_and_delta_temporality(
    database, dashboard, panel, metric, attributes, measure, expected
):
    database.execute("DELETE FROM metrics")
    attributes = {"operation": "synthetic", "outcome": "unavailable", **attributes}

    def samples(readings, *, epoch, instance="one", pid=1, version="a", temporality="cumulative", environment="production"):
        stream = repr((instance, pid, version, epoch if temporality == "cumulative" else None, temporality))
        for timestamp, value in readings:
            database.execute(
                "INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "msu-hub-bot",
                    environment,
                    version,
                    instance,
                    pid,
                    metric,
                    json.dumps(attributes),
                    json.dumps({"stream": stream, "value": value, "temporality": temporality}),
                    timestamp,
                    epoch,
                    temporality,
                    value,
                ),
            )

    samples([(121, 100), (131, 103)], epoch=0)  # Do not import a pre-window lifetime count.
    samples([(136, 2), (146, 4)], epoch=135)  # Reset within the same process.
    samples([(124, 1)], epoch=123, instance="two")  # A lone first event must count.
    samples([(124, 2)], epoch=123, pid=2)
    samples([(124, 2)], epoch=123, version="b")
    samples([(150, 2)], epoch=145, temporality="delta")
    samples([(160, 3)], epoch=155, temporality="delta")  # Delta start times change every sample.
    samples([(120, 1000)], epoch=110, environment="testing")
    sql = panel_query(dashboard, panel).replace("CAST($__from_iso_string AS TIMESTAMPTZ)", "100")
    sql = sql.replace("CAST($__to_iso_string AS TIMESTAMPTZ)", "200")
    rows = [dict(row) for row in database.execute(sql, {"resolution": "1m"})]
    assert len(rows) == 1 and rows[0][measure] == expected


@pytest.mark.parametrize(
    ("dashboard", "panel", "attributes", "measure", "expected"),
    [
        ("pulse", "handlers-card", {"boundary": "bot.handler"}, "runs", 10),
        ("pulse", "failures-card", {"boundary": "bot.handler"}, "failures", 10),
        ("usage", "popular-handlers", {"boundary": "bot.handler"}, "invocations", 10),
        ("usage", "handler-outcomes", {"boundary": "bot.handler"}, "failed", 10),
        ("failures", "dependency-outcomes", {"boundary": "provider.request"}, "failed", 10),
        ("usage", "handler-activity", {"boundary": "bot.handler"}, "invocations_per_minute", 2),
        ("pulse", "update-outcomes", {"boundary": "bot.dispatch", "operation": "dispatch"}, "updates_per_minute", 2),
        ("pulse", "archive-outcomes", {"boundary": "storage.operation", "operation": "archive.write"}, "archive_writes_per_minute", 2),
    ],
)
def test_operation_panels_count_delta_histograms_across_processes_and_intervals(database, dashboard, panel, attributes, measure, expected):
    database.execute("DELETE FROM metrics")
    database.execute("ALTER TABLE metrics ADD COLUMN histogram_count REAL")
    database.create_function("GREATEST", 2, max)
    attributes = {"operation": "synthetic", "outcome": "unavailable", **attributes}

    def point(recorded, count, *, metric="bot.operation.duration", temporality="delta", environment="production", **stream):
        database.execute(
            """INSERT INTO metrics (service_name, deployment_environment, service_version, service_instance_id, process_pid,
                   metric_name, attributes, recorded_timestamp, start_timestamp, aggregation_temporality, histogram_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "msu-hub-bot",
                environment,
                stream.get("version", "a"),
                stream.get("instance", "one"),
                stream.get("pid", 1),
                metric,
                json.dumps(stream.get("attributes", attributes)),
                recorded,
                recorded - 300,
                temporality,
                count,
            ),
        )

    point(130, 2)
    point(150, 3)  # Delta points sum without epoch or first-reading correction.
    point(124, 1, instance="two")
    point(124, 2, pid=2)
    point(124, 2, version="b")
    point(130, 1000, temporality="cumulative")  # Pre-delta lifetime totals are not counted.
    point(130, 1000, environment="testing")
    point(130, 1000, metric="bot.operations")
    point(130, 1000, attributes={**attributes, "operation": "intent.execute", "boundary": "bot.dispatch"})
    # SQLite stands in for Logfire's interval arithmetic with integer seconds.
    sql = panel_query(dashboard, panel)
    for logfire, sqlite in (
        ("EXTRACT(EPOCH FROM CAST($resolution AS INTERVAL))", "60"),
        ("EXTRACT(EPOCH FROM (recorded_timestamp - start_timestamp))", "(recorded_timestamp - start_timestamp)"),
    ):
        sql = sql.replace(logfire, sqlite)
    rows = [dict(row) for row in database.execute(sql, {"resolution": "1m"})]
    # Five-minute delta points spread their counts over the export interval, not a one-minute bucket.
    assert len(rows) == 1 and rows[0][measure] == pytest.approx(expected)


def test_delivery_table_counts_logical_calls_and_only_actionable_rejections(database):
    def record(operation, reason, *, kind="log", environment="production"):
        attributes = {"operation": operation, "telegram.method": "sendPhoto", "error.reason": reason, "outcome": "rejected"}
        database.execute(
            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("msu-hub-bot", environment, kind, "telegram.request.failed", json.dumps(attributes), 1, "trace", "span"),
        )

    record("telegram.request", "photo_dimensions_invalid")
    record("links.publish", "photo_dimensions_invalid")
    record("telegram.request", "photo_dimensions_invalid", kind="span")
    record("telegram.request", "photo_dimensions_invalid", environment="testing")
    record("telegram.request", "message_not_modified")
    record("telegram.request", "not_enough_rights")
    rows = [dict(row) for row in database.execute(panel_query("failures", "delivery-rejections"))]
    assert len(rows) == 1 and rows[0]["reason"] == "photo_dimensions_invalid" and rows[0]["rejected_calls"] == 1


@pytest.mark.parametrize("dashboard", ["pulse", "usage", "failures", "jobs"])
def test_each_dashboard_panel_is_visible_once_and_grids_do_not_overlap(dashboard):
    definition = json.loads((ROOT / (dashboard + ".json")).read_text())["definition"]
    references = []
    for layout in definition["spec"]["layouts"]:
        occupied = set()
        for item in layout["spec"]["items"]:
            references.append(item["content"]["$ref"])
            assert item["width"] > 0 and item["height"] > 0 and 0 <= item["x"] < item["x"] + item["width"] <= 24
            cells = {(x, y) for x in range(item["x"], item["x"] + item["width"]) for y in range(item["y"], item["y"] + item["height"])}
            assert not occupied & cells
            occupied.update(cells)
    assert len(references) == len(set(references))
    assert set(references) == {f"#/spec/panels/{key}" for key in definition["spec"]["panels"]}
