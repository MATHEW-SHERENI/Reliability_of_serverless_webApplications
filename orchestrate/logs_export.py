"""Copy one run's Lambda log lines to a file before the log groups disappear.

`make destroy` deletes the CloudWatch log groups, so the REPORT lines (duration,
billed time, Init) and the JSON lines of both functions are saved with each run.
Only those two kinds of line are kept; START and END lines carry nothing new.
"""
import gzip
import json

FILTER = '?REPORT ?"type"'      # REPORT lines, and the JSON lines that hold a "type" key


def export_logs(logs, log_groups, start_ms, end_ms, path):
    """Write every matching event of the groups in [start_ms, end_ms] to a .jsonl.gz file."""
    count = 0
    paginator = logs.get_paginator("filter_log_events")
    with gzip.open(path, "wt") as out:
        for group in log_groups:
            for page in paginator.paginate(logGroupName=group, startTime=int(start_ms),
                                           endTime=int(end_ms), filterPattern=FILTER):
                for event in page["events"]:
                    out.write(json.dumps({"group": group, "t": event["timestamp"],
                                          "stream": event["logStreamName"],
                                          "message": event["message"].rstrip("\n")}) + "\n")
                    count += 1
    return count
