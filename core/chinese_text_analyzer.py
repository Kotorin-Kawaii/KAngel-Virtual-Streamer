"""面向中文弹幕的轻量级话题与情感分析。

这里只负责高频、低延迟的本地信号；复杂语义仍交给正式回复链路中的模型。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ChineseTextAnalysis:
    topics: list[str]
    sentiment: float
    sentiment_signals: list[str] = field(default_factory=list)


class ChineseTextAnalyzer:
    """可解释、可测试的中文弹幕分析器。"""

    _domain_terms = (
        "直播", "游戏", "音乐", "电影", "动漫", "漫画", "偶像", "唱歌",
        "美食", "旅行", "健身", "宠物", "家庭", "朋友", "上班", "工作", "学习",
        "考试", "求职", "职场", "手机", "电脑", "互联网", "AI", "软件", "硬件",
        "心情", "恋爱", "天气", "新闻", "热点",
    )
    _topic_prefixes = (
        "你觉得", "你感觉", "想问一下", "问一下", "关于", "说说", "聊聊",
        "谈谈", "推荐一下", "推荐", "今天", "最近", "这个", "那个",
    )
    _topic_suffixes = (
        "怎么样", "怎么看", "好玩吗", "好看吗", "好听吗", "是什么", "是啥",
        "咋样", "如何", "好吗", "吗", "呢", "啊", "呀", "吧",
    )
    _topic_stopwords = {
        "什么", "怎么", "为什么", "为啥", "这个", "那个", "东西", "事情",
        "真的", "确实", "感觉", "觉得", "今天", "最近", "现在", "一下",
        "这么", "那么", "可以", "不可以", "有没有", "没什么", "只是",
    }

    _positive_terms = {
        "超喜欢": 1.5, "喜欢": 1.0, "爱了": 1.2, "爱": 0.8, "开心": 1.1,
        "快乐": 1.0, "幸福": 1.1, "感动": 0.9, "感谢": 0.8, "谢谢": 0.8,
        "支持": 0.9, "期待": 0.7, "治愈": 1.0, "舒服": 0.8, "好听": 0.9,
        "好看": 0.9, "好玩": 0.8, "厉害": 0.8, "好棒": 1.0, "真棒": 1.0,
        "完美": 0.9, "可爱": 0.9, "漂亮": 0.8, "精彩": 0.8, "不错": 0.6,
        "好": 0.35, "棒": 0.55, "赞": 0.7,
    }
    _negative_terms = {
        "不喜欢": -1.0, "讨厌": -1.0, "恶心": -1.2, "失望": -1.0, "伤心": -1.0,
        "难过": -0.9, "痛苦": -1.1, "焦虑": -0.9, "害怕": -0.8, "崩溃": -1.2,
        "破防": -0.8, "烦死": -1.2, "烦躁": -1.0, "愤怒": -1.1, "生气": -0.9,
        "好无聊": -1.1, "无聊": -0.7, "乏味": -0.7, "烂": -1.0, "糟糕": -1.0, "垃圾": -1.2,
        "拉胯": -1.0, "下头": -1.0, "麻了": -0.7, "累死": -1.0, "好累": -0.8,
        "不好": -0.8, "差劲": -0.9, "差": -0.45, "累": -0.45, "烦": -0.55,
    }
    _negations = ("不是", "没有", "不要", "不会", "不", "没", "无", "未", "别")
    _degree_terms = {
        "超级": 1.6, "特别": 1.45, "非常": 1.45, "太": 1.35, "超": 1.35,
        "真的": 1.2, "真": 1.15, "挺": 1.1, "有点": 0.75, "有一点": 0.7, "稍微": 0.65,
    }
    _contrast_markers = ("但是", "不过", "可是", "然而", "但", "却")
    _internet_scores = {
        "yyds": 1.2, "绝绝子": 0.9, "神了": 0.8, "666": 0.6,
        "笑死": 0.35, "好家伙": 0.1, "破大防": -1.0, "寄了": -0.8,
        "绷不住": -0.35, "栓Q": -0.5, "栓q": -0.5,
    }
    _streamer_scores = {
        "小天使请安": 1.2, "超绝最可爱": 1.2, "†升天†": 0.9,
        "升天": 0.6, "不要抛弃": 0.8, "蛆虫": -1.1, "闭嘴": -0.9, "滚蛋": -1.0,
    }
    _sarcasm_scores = {
        "可真有你的": -1.2, "真有你的": -1.0, "就这": -1.0, "呵呵": -1.2,
        "这也叫": -1.0, "不愧是你": -0.7, "你可太厉害了": -0.8,
        "好棒棒哦": -0.8, "谁会喜欢": -1.0, "能不能别": -0.9,
    }

    _tag_pattern = re.compile(r"#([^#\n]{1,30})#")
    _mention_pattern = re.compile(r"@([\u4e00-\u9fffA-Za-z0-9_]{1,30})")
    _title_pattern = re.compile(r"[《〈【]([^\u300b〉】\n]{1,30})[》〉】]")
    _latin_pattern = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9+_.!-]{1,30})(?![A-Za-z0-9])")
    _opinion_topic_pattern = re.compile(
        r"([\u4e00-\u9fffA-Za-z0-9+_.!-]{2,20}?)(?:真的太|真的|确实|也太|太|很|挺|有点)(?:好|烧脑|无聊|厉害|难|累|帅|可爱|好玩|好看)"
    )
    _question_topic_pattern = re.compile(
        r"([\u4e00-\u9fffA-Za-z0-9+_.!-]{2,24}?)(?:怎么样|怎么看|咋样|好玩吗|好看吗|好听吗|是什么|是啥)"
    )

    def analyze(self, content: str, max_topics: int = 5) -> ChineseTextAnalysis:
        text = self._normalize(content)
        topics = self.extract_topics(text, max_topics=max_topics)
        sentiment, signals = self.analyze_sentiment(text)
        return ChineseTextAnalysis(topics=topics, sentiment=sentiment, sentiment_signals=signals)

    def extract_topics(self, content: str, max_topics: int = 5) -> list[str]:
        text = self._normalize(content)
        candidates: list[tuple[str, float, int]] = []
        order = 0

        def add(values: Iterable[str], score: float) -> None:
            nonlocal order
            for raw_value in values:
                value = self._clean_topic(raw_value)
                if self._valid_topic(value):
                    candidates.append((value, score, order))
                    order += 1

        add(self._tag_pattern.findall(text), 5.0)
        add(self._mention_pattern.findall(text), 4.8)
        add(self._title_pattern.findall(text), 4.6)
        add(self._latin_pattern.findall(text), 3.8)
        add((term for term in self._domain_terms if term.casefold() in text.casefold()), 3.2)
        add(self._question_topic_pattern.findall(text), 3.5)
        add(self._opinion_topic_pattern.findall(text), 3.3)

        # 从逗号、句号和转折语切分的短句中生成动态候选，避免只能识别固定目录。
        dynamic_text = re.sub(r"[@#《》〈〉【】]", " ", text)
        clauses = re.split(r"[\s，。！？；：,!?;:~～]+|" + "|".join(self._contrast_markers), dynamic_text)
        for clause in clauses:
            candidate = self._dynamic_topic_from_clause(clause)
            if candidate:
                add([candidate], 2.2)

        best: dict[str, tuple[str, float, int]] = {}
        for value, score, index in candidates:
            key = value.casefold()
            current = best.get(key)
            if current is None or score > current[1]:
                best[key] = (value, score, index)

        ranked = sorted(best.values(), key=lambda item: (-item[1], item[2], -len(item[0])))
        result: list[str] = []
        for value, _, _ in ranked:
            if any(value in existing and value != existing for existing in result):
                continue
            result.append(value)
            if len(result) >= max_topics:
                break
        return result

    def analyze_sentiment(self, content: str) -> tuple[float, list[str]]:
        text = self._normalize(content)
        if not text:
            return 0.0, []

        clauses = self._weighted_clauses(text)
        total = 0.0
        signals: list[str] = []
        for clause, clause_weight in clauses:
            clause_score, clause_signals = self._score_clause(clause)
            total += clause_score * clause_weight
            signals.extend(clause_signals)

        # 反讽和主播特有表达按完整句子补充，防止被分句丢失。
        for phrase, score in self._sarcasm_scores.items():
            if phrase in text:
                total += score
                signals.append(f"反讽:{phrase}")
        if re.search(r"(?:这|就)也配?.{0,8}[？?]", text):
            total -= 0.9
            signals.append("反问:质疑")

        if not signals:
            return 0.0, []
        return max(-1.0, min(1.0, math.tanh(total / 1.8))), list(dict.fromkeys(signals))

    def _score_clause(self, clause: str) -> tuple[float, list[str]]:
        score = 0.0
        signals: list[str] = []
        occupied: list[tuple[int, int]] = []
        terms = sorted(
            (*self._positive_terms.items(), *self._negative_terms.items()),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for term, base_score in terms:
            for match in re.finditer(re.escape(term), clause, flags=re.IGNORECASE):
                span = match.span()
                if any(span[0] < end and span[1] > start for start, end in occupied):
                    continue
                occupied.append(span)
                before = clause[max(0, span[0] - 8):span[0]]
                value = base_score * self._degree(before)
                negation_count = self._negation_count(before)
                if negation_count % 2 == 1:
                    value *= -0.9
                    signals.append(f"否定:{term}")
                else:
                    signals.append(f"情感词:{term}")
                score += value

        for source_name, mapping in (
            ("网络表达", self._internet_scores),
            ("主播表达", self._streamer_scores),
        ):
            for phrase, value in mapping.items():
                if phrase.casefold() in clause.casefold():
                    score += value
                    signals.append(f"{source_name}:{phrase}")
        return score, signals

    def _weighted_clauses(self, text: str) -> list[tuple[str, float]]:
        pattern = "(" + "|".join(map(re.escape, self._contrast_markers)) + ")"
        parts = re.split(pattern, text)
        if len(parts) == 1:
            return [
                (clause, 1.0)
                for clause in re.split(r"[，。！？；,!?;]+", text)
                if clause.strip()
            ]

        weighted: list[tuple[str, float]] = []
        after_contrast = False
        for part in parts:
            if not part:
                continue
            if part in self._contrast_markers:
                after_contrast = True
                continue
            weight = 1.3 if after_contrast else 0.65
            for clause in re.split(r"[，。！？；,!?;]+", part):
                if clause.strip():
                    weighted.append((clause, weight))
        return weighted

    def _dynamic_topic_from_clause(self, clause: str) -> str:
        value = self._clean_topic(clause)
        if not value:
            return ""
        value = re.sub(
            r"(?:真的|确实|也太|太|很|挺|有点|有一点).*$",
            "",
            value,
        )
        value = re.sub(r"(?:一般|还行|差不多)$", "", value)
        value = re.sub(r"(?:我|你|他|她|它|大家|主播|超天酱)(?:们)?", "", value)
        value = re.sub(r"(?:喜欢|讨厌|觉得|感觉|想看|想听|想聊).*$", "", value)
        value = self._clean_topic(value)
        return value if 2 <= len(value) <= 16 else ""

    def _clean_topic(self, value: str) -> str:
        value = value.strip(" \t\r\n-_.,!！?？~～的了呢吗啊呀吧")
        changed = True
        while changed and value:
            changed = False
            for prefix in self._topic_prefixes:
                if value.startswith(prefix):
                    value = value[len(prefix):].strip()
                    changed = True
            for suffix in self._topic_suffixes:
                if value.endswith(suffix):
                    value = value[:-len(suffix)].strip()
                    changed = True
        return value[:30]

    def _valid_topic(self, value: str) -> bool:
        if not (2 <= len(value) <= 30) or value in self._topic_stopwords:
            return False
        if re.fullmatch(r"[0-9]+", value):
            return False
        if any(phrase in value for phrase in self._sarcasm_scores):
            return False
        sentiment_terms = (*self._positive_terms, *self._negative_terms)
        if len(value) <= 10 and any(term in value for term in sentiment_terms):
            non_sentiment = value
            for term in sorted(sentiment_terms, key=len, reverse=True):
                non_sentiment = non_sentiment.replace(term, "")
            non_sentiment = re.sub(r"(?:不是|没有|不|没|只是|有点|真|太|很)", "", non_sentiment)
            if len(non_sentiment) < 2:
                return False
        return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", value))

    def _degree(self, before: str) -> float:
        multiplier = 1.0
        for term, value in self._degree_terms.items():
            if before.endswith(term):
                multiplier = max(multiplier, value) if value >= 1 else min(multiplier, value)
        return multiplier

    def _negation_count(self, before: str) -> int:
        count = 0
        occupied: list[tuple[int, int]] = []
        for term in sorted(self._negations, key=len, reverse=True):
            for match in re.finditer(re.escape(term), before):
                if any(match.start() < end and match.end() > start for start, end in occupied):
                    continue
                occupied.append(match.span())
                count += 1
        return count

    def _normalize(self, content: str) -> str:
        return unicodedata.normalize("NFKC", content or "").strip()


chinese_text_analyzer = ChineseTextAnalyzer()
