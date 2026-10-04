import copy
from types import SimpleNamespace

import pytest

from app.ml import candidate
from app.ml.promotion import assess
from tests.unit.test_recipient_history_release import frozen_metadata, promotable_report


def valid():
    manifest = {"release_id":"fixture-v2", "payload_sha256":"test-payload", "feature_sha256":"test-contract", "queue_policy":"test-queue"}
    report = promotable_report(manifest)
    return manifest, report, frozen_metadata(manifest)


@pytest.mark.parametrize("corruption", ["duplicate", "omitted", "unrelated", "source", "truth", "protocol", "grouping", "artifact", "nan", "missing_group", "missing_metric", "group_substitution"])
def test_registered_finals_and_exact_frozen_identity_fail_closed(corruption):
    manifest, report, frozen = valid()
    reports = [copy.deepcopy(report)]
    assert assess(manifest, reports, frozen=frozen)["status"] == "PASSED"
    if corruption == "duplicate": reports.append(copy.deepcopy(report))
    elif corruption == "omitted": reports=[]
    elif corruption == "unrelated": reports[0]["final_id"]="different-final"
    elif corruption == "source": reports[0]["source_sha256"]="substitution"
    elif corruption == "truth": reports[0]["truth_sha256"]="substitution"
    elif corruption == "protocol": reports[0]["protocol_sha256"]="other-protocol"
    elif corruption == "grouping": reports[0]["grouping_sha256"]="other-grouping"
    elif corruption == "artifact": frozen["manifest_sha256"]="different-calibration-or-weights"
    elif corruption == "nan": reports[0]["results"]["motif"]["ap"]=float("nan")
    elif corruption == "missing_group": reports[0]["group_quality"]={"status":"NOT EVALUABLE"}
    elif corruption == "group_substitution": reports[0]["group_quality"]["final_id"]="unrelated-group-final"
    else: reports[0]["results"]["surge"]["p_at_100"]=None
    result=assess(manifest,reports,frozen=frozen)
    assert result["status"]=="BLOCKED" and result["errors"]


@pytest.mark.parametrize("field", ["protocol_sha256", "frozen_manifest_sha256", "registered_final_ids", "queue_policy", "feature_contract", "review_budget", "group_capacity"])
def test_runtime_rejects_transplanted_or_mismatched_approval(field):
    """Fabricated metadata only; this is NOT a model quality result."""
    manifest = {"release_id": "test-release", "payload_sha256": "weights", "feature_sha256": "features",
                "feature_contract": candidate.CONTRACT, "queue_policy": candidate.QUEUE_POLICIES[candidate.CONTRACT],
                "eligibility": "synthetic_demo"}
    report = promotable_report(manifest)
    proof = assess(manifest, [report], frozen=frozen_metadata(manifest))
    assert proof["status"] == "PASSED"
    proof["reports_sha256"] = ["UNIT_TEST_ONLY"]
    manifest["provenance"]["parent_manifest_sha256"] = proof["frozen_manifest_sha256"]
    manifest.update(eligibility="validated_candidate", promotion={"representative_labels": True, "domain": "test-only", "quality_validation": proof})
    case = SimpleNamespace(scoring_mode="auto_eligible", candidate_domain="test-only")
    assert candidate.eligibility(manifest, case, finding_budget=1.)[0]
    proof[field] = ["substitution"] if field == "registered_final_ids" else "substitution"
    assert not candidate.eligibility(manifest, case, finding_budget=1.)[0]
