"""「学习文风」作业（learn_job）测试用的确定性假模型。

按 ``request.node_id`` 分发，从不可信数据边界里读出载荷，给出**合法**的结构化输出：

- 四个抽取节点：每维 2 条观察 + 1 条「作者不这么写」，每条两处引文逐字取自样本里【p】标出的段；
- 文风卡合成：每维 1–2 条 do（refs 指向观察）、有避免的维 1 条 avoid，外加气质、规划手法、标题与概述；载荷里有
  上一版文风卡（``previous_card``）时每条 do 都 ``keep`` 上一版同一维同一类的第 i 句（测「在上一版上修订」）；
- 通读全书的归并（同一个合成节点、载荷带 ``findings``）：陈述一字不差的发现归成一组；
- 按场面的写法（同一个合成节点、载荷带 ``situation``）：前两条发现各写一句（refs 指向它），有上一版的就 keep；
- 叙事层在载荷有 ``scope`` 时顺带交结构：每个片段中间那一段换场（地点），每个整章片段一条章法（章号是 4 的倍数 =
  高潮，其余推进；单数章悬念未解、双数章反转；2 条线、1 个视点）；
- 受保护专名：候选里属于 ``protected`` 的词（kind 按 ``kinds``，缺省 person）；
- 窗口标签：每窗一项（场面 / 情绪从词表里挑、手法取手法表第一个、gist 带一个人名——测专名替换）。

``script(node_id, payload, call_no)`` 可以改写某一次调用：返回 ``"fail"`` 抛供应商错误、``"crash"`` 抛
``SimulatedCrash``（BaseException，模拟进程被杀）、一个 dict 当作结构化输出原样返回，或 ``None`` 走默认。
``gate_node`` 让某个节点的第 ``gate_call`` 次调用卡在闸门上（测取消 / 所有权）。
"""

from __future__ import annotations

import json
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

from tests.accounted_llm_fakes import AccountedGenerateMixin

_BOUNDARY_RE = re.compile(r"\[UNTRUSTED_REFERENCE_DATA:[^\]]+\]\n")
_PARA_RE = re.compile(r"^【(\d+)】(.*)$")

EXTRACT_PREFIX = "style_ref_extract_"
NODE_SYNTH = "style_ref_synthesize_profile"
NODE_PROTECTED = "style_ref_protected_terms"
NODE_TAGS = "style_ref_tag_windows"


class SimulatedCrash(BaseException):
    """模拟进程在调用中途被杀（jobs 框架只接 Exception，BaseException 会穿出去、作业留在 running）。"""


def payload_of(request: Any) -> dict[str, Any]:
    user = request.messages[-1]["content"]
    opening = _BOUNDARY_RE.search(user)
    if opening is None:
        return {}
    closing = user.find("\n[/UNTRUSTED_REFERENCE_DATA]", opening.end())
    return json.loads(user[opening.end():closing])


def paragraphs_of(payload: dict[str, Any]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for window in payload.get("windows") or []:
        for line in str(window.get("text") or "").split("\n"):
            match = _PARA_RE.match(line)
            if match:
                out.append((int(match.group(1)), match.group(2)))
    return out


def _quote(text: str, offset: int = 0) -> str:
    body = text.strip()
    start = min(offset, max(0, len(body) - 8))
    return body[start : start + 8] if len(body) >= 8 else body


def default_extract(payload: dict[str, Any], *, tag: str = "") -> dict[str, Any]:
    paras = [(p, t) for p, t in paragraphs_of(payload) if len(t.strip()) >= 8]
    dims = payload.get("dimensions") or []
    out = []
    for d_index, dim in enumerate(dims):
        key = dim["key"]
        label = dim["label"]

        def evidence(i: int) -> list[dict[str, Any]]:
            a = paras[(d_index * 5 + i * 2) % len(paras)]
            b = paras[(d_index * 5 + i * 2 + 1) % len(paras)]
            return [{"p": a[0], "quote": _quote(a[1])}, {"p": b[0], "quote": _quote(b[1], 2)}]

        out.append(
            {
                "dimension": key,
                "model_default": f"通用写法在{label}上平铺直叙{tag}",
                "devices": [f"{label[:2]}手法"],
                "distinctiveness": round(0.9 - d_index * 0.1, 2),
                "observations": [
                    {
                        "statement": f"{label}上作者的第{'一二'[i]}种做法{tag}",
                        "confidence": "high",
                        "distinctiveness": 0.8,
                        "evidence": evidence(i),
                    }
                    for i in range(2)
                ],
                "avoid": [
                    {
                        "statement": f"{label}上作者不照通用写法来{tag}",
                        "confidence": "medium",
                        "distinctiveness": 0.6,
                        "evidence": evidence(3),
                    }
                ],
            }
        )
    return {"dimensions": out}


def default_structure(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("scope"):
        return {"scene_starts": [], "chapters": []}
    starts, chapters = [], []
    for window in payload.get("windows") or []:
        numbers = [int(m.group(1)) for line in str(window.get("text") or "").split("\n") if (m := _PARA_RE.match(line))]
        if len(numbers) >= 4:
            starts.append({"p": numbers[len(numbers) // 2], "shift": "地点"})
        if window.get("position") == "整章":
            chapter = int(window.get("chapter") or 0)
            chapters.append(
                {
                    "window": window["window"],
                    "function": "高潮" if chapter % 4 == 0 else "推进",
                    "hook": "悬念未解" if chapter % 2 else "反转",
                    "threads": 2,
                    "povs": 1,
                }
            )
    return {"scene_starts": starts, "chapters": chapters}


def default_situation(payload: dict[str, Any]) -> dict[str, Any]:
    findings = payload.get("findings") or []
    previous = payload.get("previous_lines") or []
    situation = payload.get("situation") or ""
    return {
        "lines": [
            {
                "text": f"写{situation}时先让人物说半句闲话再进正事（第{'一二'[i]}条）",
                "refs": [findings[i]["ref"]],
                "keep": previous[i]["id"] if i < len(previous) else "",
            }
            for i in range(min(2, len(findings)))
        ]
    }


def default_consolidate(payload: dict[str, Any]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[str]] = {}
    for finding in payload.get("findings") or []:
        groups.setdefault((finding["kind"], finding["statement"]), []).append(finding["ref"])
    return {
        "model_default": "通用写法平铺直叙",
        "devices": list(payload.get("devices") or [])[:2],
        "distinctiveness": 0.7,
        "groups": [
            {"kind": kind, "statement": statement, "refs": refs, "distinctiveness": 0.6}
            for (kind, statement), refs in groups.items()
        ],
    }


def _previous_lines(payload: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for entry in payload.get("previous_card") or []:
        for line in entry.get("lines") or []:
            out.setdefault((entry["dimension"], line["kind"]), []).append(line["id"])
    return out


def default_card(payload: dict[str, Any]) -> dict[str, Any]:
    dims = []
    previous = _previous_lines(payload)
    for index, dim in enumerate(payload.get("dimensions") or []):
        observations = dim.get("observations") or []
        avoid = dim.get("avoid") or []
        if not observations:
            continue
        label = dim["label"]
        kept = previous.get((dim["dimension"], "do"), [])
        do = [
            {
                "text": f"代替平铺直叙，{label}照作者的做法写（第{'一二'[i]}条，换个说法）"
                if i < len(kept)
                else f"代替平铺直叙，{label}照作者的做法写（第{'一二'[i]}条）",
                "refs": [obs["ref"]],
                "mandatory": index == 0 and i == 0,
                "distinctiveness": 0.9 - index * 0.05,
                "keep": kept[i] if i < len(kept) else "",
            }
            for i, obs in enumerate(observations[:2])
        ]
        dims.append(
            {
                "dimension": dim["dimension"],
                "summary": f"{label}有作者自己的路数",
                "model_default": dim.get("model_default") or "通用写法",
                "distinctiveness": max(0.1, 0.9 - index * 0.05),
                "devices": list(dim.get("devices") or [])[:1],
                "do": do,
                "avoid": (
                    [{"text": f"不按通用写法处理{label}", "refs": [avoid[0]["ref"]], "mandatory": False, "distinctiveness": 0.5}]
                    if avoid
                    else []
                ),
            }
        )
    return {
        "profile_title": "市井口吻的短句叙事",
        "qualitative_summary": "作者用市井口吻讲故事，危急时刻也要开玩笑，玩笑底下压着孤独。",
        "temperament": ["越危险越要开玩笑", "玩笑底下压着孤独"],
        "dimensions": dims,
        "planning_guidance": ["开场：先抛一句闲话再进正事", "收场：落在一个具体的小动作上"],
    }


class FakeLearnLLM(AccountedGenerateMixin):
    def __init__(
        self,
        *,
        script: Callable[[str, dict[str, Any], int], Any] | None = None,
        protected: tuple[str, ...] = (),
        kinds: dict[str, str] | None = None,
        gist_name: str = "",
        delay: float = 0.0,
        gate_node: str | None = None,
        gate_call: int = 1,
    ) -> None:
        self.script = script
        self.protected = tuple(protected)
        self.kinds = dict(kinds or {})
        self.gist_name = gist_name
        self.delay = delay
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self.extra_instructions: list[tuple[str, bool]] = []
        self.inflight = 0
        self.max_inflight: dict[str, int] = {}
        self.gate_node = gate_node
        self.gate_call = gate_call
        self.gate = threading.Event()
        self.entered = threading.Event()
        self._node_calls: dict[str, int] = {}

    def count(self, prefix: str) -> int:
        return sum(1 for node in self.calls if node.startswith(prefix))

    def generate(self, request):  # noqa: ANN001
        node = str(getattr(request, "node_id", "") or "")
        payload = payload_of(request)
        user = request.messages[-1]["content"]
        with self.lock:
            self.calls.append(node)
            self.payloads.append((node, payload))
            self.extra_instructions.append((node, "【重试说明】" in user))
            call_no = len(self.calls)
            self._node_calls[node] = self._node_calls.get(node, 0) + 1
            node_call = self._node_calls[node]
            self.inflight += 1
            self.max_inflight[node] = max(self.max_inflight.get(node, 0), self.inflight)
        try:
            if self.gate_node and node.startswith(self.gate_node) and node_call == self.gate_call:
                self.entered.set()
                assert self.gate.wait(timeout=30)
            if self.delay:
                time.sleep(self.delay)
            action = self.script(node, payload, call_no) if self.script else None
            if action == "fail":
                raise RuntimeError("relay down")
            if action == "crash":
                raise SimulatedCrash(node)
            structured = action if isinstance(action, dict) else self._respond(node, payload)
            return SimpleNamespace(
                structured_output=structured,
                text=json.dumps(structured, ensure_ascii=False),
                usage={},
                finish_reason="stop",
                request_id=None,
                provider="fake",
                model="fake",
                raw_response={},
                response_format="json_object",
            )
        finally:
            with self.lock:
                self.inflight -= 1

    def _respond(self, node: str, payload: dict[str, Any]) -> dict[str, Any]:
        if node.startswith(EXTRACT_PREFIX):
            output = default_extract(payload)
            if node == f"{EXTRACT_PREFIX}narrative":
                output.update(default_structure(payload))
            return output
        if node == NODE_SYNTH:
            if "situation" in payload:
                return default_situation(payload)
            return default_consolidate(payload) if "findings" in payload else default_card(payload)
        if node == NODE_PROTECTED:
            terms = [
                {"term": c["term"], "kind": self.kinds.get(c["term"], "person")}
                for c in payload.get("candidates") or []
                if c["term"] in self.protected
            ]
            return {"terms": terms}
        if node == NODE_TAGS:
            vocabulary = [str(d.get("key") or "") for d in payload.get("dimension_vocabulary") or [] if isinstance(d, dict)]
            return {
                "windows": [
                    {
                        "window": w["window"],
                        "situations": ["日常闲谈", "不在词表里的场面"],
                        "moods": ["平静"],
                        "dimensions": vocabulary[:1],
                        "gist": f"{self.gist_name}在院子里说话" if self.gist_name else "主角在院子里说话",
                    }
                    for w in payload.get("windows") or []
                ]
            }
        return {}


__all__ = [
    "FakeLearnLLM",
    "NODE_PROTECTED",
    "NODE_SYNTH",
    "NODE_TAGS",
    "SimulatedCrash",
    "default_card",
    "default_consolidate",
    "default_situation",
    "default_structure",
    "default_extract",
    "paragraphs_of",
    "payload_of",
]
