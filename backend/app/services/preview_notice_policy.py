"""预览重算时保留不可由活动/交通推导的既有信息提示，并绑定当前版本。"""
PERSISTENT_PREVIEW_NOTICE_CODES = frozenset({
    "INITIAL_ACTIVITY_OMITTED", "DISPATCH_COLLECTION_PARTIAL",
    "ENDPOINT_ARRIVAL_UNKNOWN", "ENDPOINT_DEPARTURE_UNKNOWN",
})


def preserve_preview_notices(report, previous_report):
    known = {issue.issue_id for issue in report.issues}
    notices = tuple(issue.model_copy(update={"based_on_revision": report.based_on_revision})
        for issue in previous_report.issues if issue.code in PERSISTENT_PREVIEW_NOTICE_CODES and issue.issue_id not in known)
    if not notices:
        return report
    return report.model_copy(update={
        "issues": (*report.issues, *notices),
        "days": tuple(day.model_copy(update={
            "issue_ids": (*day.issue_ids, *(issue.issue_id for issue in notices if issue.day_id == day.day_id)),
        }) for day in report.days),
    })
