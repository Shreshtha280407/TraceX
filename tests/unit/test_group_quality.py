from app.engine.investigations import POLICY, PROCEDURE_SHA256
from scripts.group_quality import evaluate


def test_group_truth_is_episode_proposition_not_any_positive_member():
    groups=[{"group_id":"a","family":"peeling_chain/v1"},{"group_id":"b","family":"peeling_chain/v1"}]
    truth={"evaluation_only":True,"do_not_ingest":True,"grouping_sha256":PROCEDURE_SHA256,"proposition":POLICY["group_target"],
           "episodes":[{"episode_id":"positive","rule_id":"peeling_chain","transactions":["p1","p2"],"requires_review":True},
                       {"episode_id":"benign","rule_id":"peeling_chain","transactions":["n1"],"requires_review":False,"benign_control":"merchant"}]}
    result=evaluate(groups,{"a":["p1","p2"],"b":["n1"]},truth,capacity=2)
    assert result["status"]=="EVALUATED" and result["precision_at_capacity"]==.5
    mixed=evaluate(groups,{"a":["p1","n1"],"b":["unknown"]},truth,capacity=2)
    assert mixed["status"]=="NOT EVALUABLE" and mixed["precision_at_capacity"] is None
    assert mixed["mixed_groups"]==mixed["overmerged_groups"]==mixed["unknown_groups"]==1


def test_insufficient_k_and_fragmentation_are_reported_not_passing():
    truth={"evaluation_only":True,"do_not_ingest":True,"grouping_sha256":PROCEDURE_SHA256,"proposition":POLICY["group_target"],
           "episodes":[{"episode_id":"episode","rule_id":"peeling_chain","transactions":["a","b"],"requires_review":True}]}
    groups=[{"group_id":"a","family":"peeling_chain/v1"},{"group_id":"b","family":"peeling_chain/v1"}]
    result=evaluate(groups,{"a":["a"],"b":["b"]},truth)
    assert result["p_at_100"] is None and result["fragmented_episodes"]=={"episode":2}
