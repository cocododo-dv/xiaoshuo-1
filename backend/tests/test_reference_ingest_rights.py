"""Wave 7（§5.9 / §11 规则 9）：导入时记录用户对文本的分析+发送权限声明。

不得默认拥有云端发送权；非本地策略必须显式声明且 send_rights=True；local_only
未声明仍记录 `{declared: false}`。
"""
from __future__ import annotations

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.ingest import IngestService

SAMPLE = "第一段参考文字。\n\n第二段参考文字，用于风格分析。"


def _ingest(seed, *, cloud_policy, rights=None):
    with SessionLocal() as session:
        svc = IngestService(session, llm_enabled=False)
        result = svc.ingest_upload(
            raw_bytes=(SAMPLE + seed).encode("utf-8"),
            file_name=f"s_{seed}.txt", title="t", author_label="a",
            cloud_policy=cloud_policy, rights_declaration=rights,
        )
        session.commit()
        return result.book


def test_rights_declaration_recorded_in_stats():
    book = _ingest("r1", cloud_policy="local_only",
                   rights={"analysis_rights": True, "send_rights": False, "declared_by": "作者"})
    decl = book.stats_json["rights_declaration"]
    assert decl["analysis_rights"] is True
    assert decl["send_rights"] is False
    assert decl["declared_by"] == "作者"
    assert decl["declared"] is True
    assert decl["declared_at"]


def test_undeclared_records_declared_false():
    book = _ingest("r2", cloud_policy="local_only", rights=None)
    decl = book.stats_json["rights_declaration"]
    assert decl["declared"] is False


def test_no_send_rights_but_cloud_policy_rejected():
    # 声明无发送权却选云端策略 → 矛盾，拒绝（§11 规则 9：不得默认云端发送权）
    with pytest.raises(DomainError) as exc:
        _ingest("r3", cloud_policy="allow_full_cloud",
                rights={"analysis_rights": True, "send_rights": False})
    # 请求本身不合法:与「未声明」同一个 400 码;STYLE_REFERENCE_SEND_RIGHTS_REQUIRED 只表示
    # 已有的书缺发送权(一律 409,见 policy / test_style_reference_policy)
    assert exc.value.code == "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED"
    assert exc.value.status_code == 400
    assert exc.value.details["reason"] == "send_rights_false"


@pytest.mark.parametrize("cloud_policy", ["segments_only", "allow_full_cloud"])
def test_send_rights_with_cloud_policy_ok(cloud_policy):
    book = _ingest(f"r4-{cloud_policy}", cloud_policy=cloud_policy,
                   rights={"analysis_rights": True, "send_rights": True})
    assert book.stats_json["rights_declaration"]["send_rights"] is True


@pytest.mark.parametrize("cloud_policy", ["segments_only", "allow_full_cloud"])
@pytest.mark.parametrize(
    "rights",
    [
        None,
        {"declared": False, "analysis_rights": True, "send_rights": True},
    ],
    ids=["missing", "explicitly-undeclared"],
)
def test_cloud_policy_requires_explicit_send_rights_declaration(cloud_policy, rights):
    with pytest.raises(DomainError) as exc:
        _ingest(f"r5-{cloud_policy}-{rights is None}", cloud_policy=cloud_policy, rights=rights)
    assert exc.value.code == "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED"
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("send_rights", "false"),
        ("send_rights", 1),
        ("declared", "false"),
        ("declared", 1),
        ("analysis_rights", "false"),
        ("analysis_rights", 1),
    ],
)
def test_cloud_policy_rejects_non_boolean_rights_values(field, value):
    rights = {
        "declared": True,
        "analysis_rights": True,
        "send_rights": True,
        field: value,
    }
    with pytest.raises(DomainError) as exc:
        _ingest(f"r6-{field}-{value}", cloud_policy="segments_only", rights=rights)
    assert exc.value.code == "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID"
    assert exc.value.status_code == 400
