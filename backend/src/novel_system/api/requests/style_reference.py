"""风格参考接口的请求体：参考书（重新分类、批量删、服务器路径导入）、学习文风、文风卡行 ✓ / ✗ 与禁用词、
绑定（用于作品、改配置）、对照检查。

``ReclassifyRequest`` 与 ``CheckRequest`` 只封闭多余字段、不开严格类型（与原接口一致）；其余是严格模型。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novel_system.api.requests.common import BoundedJsonObject, StrictRequestModel
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    MAX_SAMPLE_WINDOWS,
    MIN_SAMPLE_WINDOWS,
)
from novel_system.services.style_reference.check_job import CHECK_MAX_TEXT_CHARS


class ReclassifyRequest(BaseModel):
    """重新分类(后台分类作业)。

    - 缺省(``mode="reclassify"``):**破坏式**——先清掉这本书的全部派生数据(抽取 / 画像 / 绑定 /
      禁用词 / 作业 / 窗口索引),再从头分类;
    - ``mode="retype"``:**就地重标段落类型**——正文不变,派生数据与绑定全部保留,书保持可用;
    - ``resume=true``:把最近一次失败 / 取消 / 中断的分类作业从游标续跑(进程重启之后也行)。
    """

    model_config = ConfigDict(extra="forbid")
    resume: bool = False
    mode: Literal["reclassify", "retype"] = "reclassify"


class BulkDeleteRequest(StrictRequestModel):
    """书库多选删除:一次最多 100 本;重复的 id 只删一次。"""

    book_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(min_length=1, max_length=100)


class ImportPathRequest(StrictRequestModel):
    file_path: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=512)
    author_label: str | None = Field(default=None, max_length=255)
    cloud_policy: Literal["allow_full_cloud", "segments_only", "local_only"]
    # Wave 7 §5.9 — 导入权属声明 {analysis_rights, send_rights, declared_by}
    rights_declaration: BoundedJsonObject | None = None


class LearnRequest(StrictRequestModel):
    """「学习文风」:建一个学习作业(或 ``resume`` 续上最近一次失败 / 取消 / 中断的)。

    ``profile_id``:要就地更新的画像(缺省:这本书有绑定的 / active 的 / 最近更新的那份;没有画像就新建);
    ``force``:正文少到四层都被评估为 skip 时仍要学(界面上的「仍然学习」);
    ``retag``:给全书每个窗口重打标签(缺省只补标签版本不是当前版本的窗口)。
    """

    profile_id: str | None = Field(default=None, max_length=128)
    resume: bool = False
    force: bool = False
    retag: bool = False


class CardLineStateRequest(StrictRequestModel):
    """文风卡一句的状态:``pinned`` 永远带上 / ``excluded`` 不再用 / ``null`` 清掉。"""

    state: Literal["pinned", "excluded"] | None = None


class BannedTermCreateRequest(StrictRequestModel):
    """禁用词登记:generation=起草时不许出现(进红线);extraction=学习时滤掉含这个词的段落。"""

    term: str = Field(min_length=1, max_length=512)
    replacement_hint: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="generation", min_length=1, max_length=64)


DimensionKey = Literal[ALL_DIMENSIONS]  # type: ignore[valid-type]


class BindingConfigBody(StrictRequestModel):
    """v3 绑定配置(四键都可省:省掉的键保留这条绑定已有的值,新建时取默认)。"""

    reference_mode: Literal["full", "samples_only", "card_only"] | None = None
    sample_windows: int | None = Field(default=None, ge=MIN_SAMPLE_WINDOWS, le=MAX_SAMPLE_WINDOWS)
    dimension_states: dict[DimensionKey, Literal["emphasize", "normal", "exclude"]] | None = Field(
        default=None, max_length=len(ALL_DIMENSIONS)
    )
    draft_mode: Literal["style_first", "neutral_first"] | None = None

    def as_patch(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class ApplyProfileRequest(StrictRequestModel):
    """把画像用于一个目标:作品(``project``)/ 某一场(``scene``)/ 某个角色(``character``)。"""

    scope: Literal["project", "scene", "character"]
    scope_ref_id: str = Field(min_length=1, max_length=255)
    config: BindingConfigBody | None = None


class BindingPatchRequest(StrictRequestModel):
    config: BindingConfigBody


class CheckRequest(BaseModel):
    """对照检查：``text`` 与 ``scene_id`` 恰好给一个；文字要说对照哪份参考（``profile_id`` 或 ``project_id``）。"""

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=CHECK_MAX_TEXT_CHARS)
    scene_id: str | None = Field(default=None, max_length=255)
    profile_id: str | None = Field(default=None, max_length=255)
    project_id: str | None = Field(default=None, max_length=255)
