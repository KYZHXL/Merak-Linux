"""人设一致性检查：三层阶梯式 taboo 越界判定。

背景（实验证据）：纯向量相似度分不清"谈熬夜"和"承认熬夜"——taboo 是规则（动作），
不是主题（话题）。子串匹配只覆盖少量变体。LLM 判定准但贵。

三层阶梯（成本从低到高）：
- L1 子串层：taboo.keywords（含剥离框架词的默认词）+ 同义词表扩展。命中 → 违规
- L2 模板层：内置"行为动作"变体模式 + 角色 examples。命中 → 违规
- L3 LLM 层：L1/L2 命中"模糊区"（有主题词但无法判断是否主动违规）才调 LLM
      function calling 返回 {violates, reason}

关键设计：L1/L2 快路径负责"确定违规"和"确定不违规"；只有模糊区才上 L3，
把 API 调用压到最低，同时保住准确率上限。
"""
from __future__ import annotations

import re
from typing import Optional

from .. import llm as llm_mod
from .models import Character, MemoryEntry, Taboo

# taboo 里的常见框架词，从"绝不承认自己熬夜"剥离出内容关键词"熬夜"
_FRAMING_WORDS = (
    "绝不", "不会", "不能", "不可以", "不承认", "承认", "主动", "永远", "任何", "从不", "从来",
    "自己", "别人", "他人", "任何人", "对", "关于",
)

# 内置"行为动作"同义词表：L1 扩展用
_ACTION_SYNONYMS = {
    "熬夜": ["通宵", "熬", "失眠", "没睡好", "夜猫子"],
    "睡": ["合眼", "眯", "躺下", "就寝"],
    "谈": ["提起", "说到", "提及", "聊起", "谈起"],
    "收": ["接受", "答应", "应下"],
    "说谎": ["撒谎", "编", "骗"],
    "哭": ["流泪", "眼眶湿", "掉眼泪"],
}


def _strip_framing(text: str) -> str:
    for w in _FRAMING_WORDS:
        text = text.replace(w, "")
    return text


def default_keywords(taboo: Taboo) -> list[str]:
    """从 taboo.text 剥离框架词，查同义词表扩展，得到 L1 关键词列表。"""
    core = _strip_framing(taboo.text)
    tokens = [t for t in re.split(r"[，。！？、\s,.!?；;：:()（）]+", core) if 1 < len(t) <= 8]
    kws = list(taboo.keywords)
    for t in tokens:
        if t not in kws:
            kws.append(t)
        kws.extend(_ACTION_SYNONYMS.get(t, []))
    return [k for k in kws if k]


# ---- 内置变体模板（L2）----

# 模式：(正则, 说明)。命中即视为违规变体。
_VARIANT_PATTERNS = [
    (r"{时间}点才(?:睡|合眼|眯|躺)", "凌晨X点才睡"),
    (r"凌晨.{0,4}(?:才)?(?:睡|合眼|眯|躺)", "凌晨才睡"),
    (r"(?:通宵|熬了一宿|一夜没睡|整晚没睡)", "通宵没睡"),
    (r"顶着.{0,3}黑眼圈", "黑眼圈"),
    (r"说(?:自己)?(?:通宵|熬夜|熬了)", "自述熬夜"),
]


def _template_hits(summary: str) -> bool:
    for pattern, _desc in _VARIANT_PATTERNS:
        if re.search(pattern, summary):
            return True
    return False


def _template_behavior_words(summary: str) -> list[str]:
    """返回命中的模板对应的行为词（供主体检查用）。"""
    words = []
    for pattern, desc in _VARIANT_PATTERNS:
        if re.search(pattern, summary):
            # 提取模板里的行为词（睡/合眼/眯/躺/通宵等）
            for w in ("睡", "合眼", "眯", "躺", "通宵", "熬夜", "黑眼圈"):
                if w in desc or (w in summary and re.search(pattern, summary)):
                    if w not in words:
                        words.append(w)
    return words


# ---- L3 LLM 判定器 ----

JUDGE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "judge_taboo",
            "description": (
                "判定一条记忆是否构成角色违反禁忌。禁忌是角色绝不允许出现的行为。"
                "关键：区分'角色主动做出该行为'（违规）与'别人提到相关话题/被问'（不违规）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "violates": {
                        "type": "boolean",
                        "description": "该记忆是否构成角色主动违反禁忌",
                    },
                    "reason": {"type": "string", "description": "判断依据，一句话"},
                },
                "required": ["violates", "reason"],
            },
        },
    }
]


class TabooViolationChecker:
    """三层阶梯式 taboo 判定器。llm 传 None 时只走 L1/L2（纯规则模式）。"""

    def __init__(self, taboos: list[Taboo], llm: Optional["llm_mod.LLMClient"] = None,
                 use_llm: bool = True, character_name: str = ""):
        self.taboos = taboos
        self.llm = llm
        self.use_llm = use_llm
        self.character_name = character_name
        self._cache: dict[str, bool] = {}
        self.llm_calls = 0  # 统计 L3 调用次数（性能验证用）

    # ---- 对外接口 ----

    def violates(self, summary: str, hooks: Optional[list[str]] = None) -> bool:
        """判定记忆摘要是否违规（快路径，不含 L3）。hooks 提供额外线索。"""
        text = summary + " " + " ".join(hooks or [])
        if text in self._cache:
            return self._cache[text]
        violates, _amb = self._check(text)
        self._cache[text] = violates
        return violates

    # ---- 三层判定 ----

    def _check(self, text: str) -> tuple[bool, bool]:
        """返回 (violates, ambiguous)。

        violates=True：快路径确定违规（角色主动做了 taboo 行为）
        ambiguous=True：命中 taboo 主题词，但无法确定是否角色主动违规 → 需 L3
        """
        lowered = text.lower()
        for taboo in self.taboos:
            kws = default_keywords(taboo)
            topic_hit = any(kw.lower() in lowered for kw in kws)
            # L2 模板：内置变体模式（"凌晨X点才睡"这类本身就是行为描述）
            template_hit = _template_hits(lowered)
            # L2 示例：角色定义的"也算违规"表述，与摘要共享关键行为词
            example_hit = any(self._example_related(ex, text) for ex in taboo.examples)

            if template_hit or example_hit:
                # 模板/示例描述的是行为本身，还需确认主体是角色
                # 模板命中的行为词（睡/合眼/眯等）也要纳入主体检查
                tmpl_kws = _template_behavior_words(lowered)
                if example_hit:
                    # 示例命中时，用示例的显著子串做行为词（比 taboo 关键词更具体）
                    ex_kws = self._example_behavior_words(text)
                else:
                    ex_kws = []
                behavior = list(dict.fromkeys(kws + tmpl_kws + ex_kws))
                if self._subject_is_character(text, behavior):
                    return True, False
                # 行为词命中但主体不明 → 模糊区
                return False, True

            if topic_hit:
                # 有主题词（"熬夜"），需区分：角色主动承认 vs 别人问/讨论
                if self._subject_is_character(text, kws):
                    return True, False
                return False, True  # 模糊区，交给 L3
        return False, False

    # ---- 主体判断（违规行为是否由角色做出） ----

    def _subject_is_character(self, text: str, behavior_kws: list[str]) -> bool:
        """启发式判断：摘要中的 taboo 行为主体是否疑似角色本人。

        behavior_kws 是 taboo 的关键词（如"熬夜/通宵/手术"），作为行为词。
        先排除"别人问/说/讨论角色话题"的句式（这类明确不违规），
        再检查角色名/代词后紧跟行为词（"老猫熬夜""小言提起手术"）。
        命中即视为角色主动；否则视为别人提/被问（放行或模糊区）。
        """
        name = re.escape(self.character_name) if self.character_name else "老猫|小言"
        subject = f"(?:{name}|角色|自己|我|本角色)"
        # 别人问/说/提到角色（明确不违规）：
        #   要求"问/说/提到"前是"别人/成员名"（非角色名/代词），后面接"角色名+话题"
        #   "老猫说自己通宵"（老猫说→自己）不属于此类，因为主语是角色名
        asker = r"(?:阿伟|小美|大壮|老王|大熊|小明|别人|大家|群友|某人|这位|粉丝|网友)"
        if re.search(rf"{asker}(?:问|问起|提到|说|说起了|说|让|请|求){subject}(?:是不是|有没有|昨晚|最近|也|还|总|讲讲|说说|聊)?", text):
            return False
        # 行为词正则：角色名/代词后紧跟（0-8 字符内）任一 taboo 关键词
        # 单字行为词（睡/熬等）也允许——模板提取的词常是单字
        behavior = "|".join(re.escape(k) for k in behavior_kws if k.strip())
        if not behavior:
            return False
        patterns = [
            rf"{subject}(?:昨晚|今早|刚刚|上周|最近|又|主动|在群里)?[的,，]?.{{0,8}}(?:{behavior})",
            rf"(?:承认|自述|说自己|主动说起|提起)(?:昨晚|曾经|最近|上次)?(?:{behavior})",
        ]
        return any(re.search(p, text) for p in patterns)

    def _example_related(self, example: str, text: str) -> bool:
        """判定记忆摘要与某示例是否描述同一违规行为。

        规则：剥离框架词/角色名后，示例与摘要共享 ≥1 个 ≥3 字的显著子串
        （如"舍不得""通宵打游戏"）即相关。示例是"也算违规"的明确表述，
        摘要复现其核心子串大概率是同类事件。
        """
        skip = {self.character_name} | {"老猫", "小言", "阿伟", "小美", "大壮", "老王", "大熊", "小明"}
        ex_core = _strip_framing(example)
        txt_core = _strip_framing(text)
        for w in skip:
            ex_core = ex_core.replace(w, "")
            txt_core = txt_core.replace(w, "")
        if len(ex_core) < 3 or len(txt_core) < 3:
            return False
        # 枚举示例的 ≥3 字子串，检查是否出现在摘要中
        for i in range(len(ex_core) - 2):
            seg = ex_core[i : i + 3]
            if seg in txt_core:
                return True
        return False

    def _example_behavior_words(self, text: str) -> list[str]:
        """从命中的示例里提取显著子串作为行为词（供主体检查）。"""
        words = []
        for taboo in self.taboos:
            for ex in taboo.examples:
                if not self._example_related(ex, text):
                    continue
                skip = {self.character_name} | {"老猫", "小言", "阿伟", "小美", "大壮", "老王", "大熊", "小明"}
                ex_core = _strip_framing(ex)
                for w in skip:
                    ex_core = ex_core.replace(w, "")
                # 取示例中最长的 3-4 字核心片段作为行为词
                for start in range(0, len(ex_core) - 2):
                    seg = ex_core[start : start + 3]
                    if seg in text and seg not in words:
                        words.append(seg)
        return words

    # ---- L3 模糊区（预留，需 LLM） ----

    def violates_ambiguous(self, summary: str, taboo: Taboo) -> Optional[bool]:
        """模糊区判定：有主题词但分不清是否主动违规。llm 不可用时返回 None（放行）。"""
        if not self.use_llm or self.llm is None:
            return None
        try:
            msgs = [
                llm_mod.LLMMessage(role="system", content=(
                    f"你是人设一致性检查器。角色禁忌：{taboo.text}。"
                    "判定下面这条记忆是否构成角色主动违反禁忌。"
                    "注意区分'角色主动做出该行为'（违规）与'别人提到相关话题/被问'（不违规）。"
                )),
                llm_mod.LLMMessage(role="user", content=summary),
            ]
            result = self.llm.chat_structured(msgs, JUDGE_TOOL, temperature=0)
            self.llm_calls += 1
            return bool(result.get("violates"))
        except llm_mod.LLMError:
            return None

    def check_entry(self, entry: MemoryEntry) -> bool:
        """对记忆条目做完整判定（含 L3 模糊区兜底）。"""
        text = entry.summary + " " + " ".join(entry.hooks)
        if text in self._cache:
            return self._cache[text]
        violates, ambiguous = self._check(text)
        if violates:
            self._cache[text] = True
            return True
        if ambiguous and self.taboos:
            # 模糊区：调一次 L3 LLM 兜底（多个 taboo 时取首个判定，控制 API 成本）
            verdict = self.violates_ambiguous(entry.summary, self.taboos[0])
            if verdict:
                self._cache[text] = True
                return True
        self._cache[text] = False
        return False


def build_checker(character: Character, llm=None, mode: str = "auto") -> Optional[TabooViolationChecker]:
    """构造 checker。mode='off' 返回 None（不检查）；'auto'/'llm' 开启。"""
    if mode == "off" or not character.taboos:
        return None
    return TabooViolationChecker(character.taboos, llm=llm, use_llm=mode in ("auto", "llm"),
                                 character_name=character.name)
