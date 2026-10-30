"""Discover names and compounds the search dictionary splits, from a snapshot.

    uv run --no-sync python scripts/discover_search_words.py \\
        --dsn postgresql://... --schema rag_s... --out agent/src/search_words.txt
    cp agent/src/search_words.txt indexer/src/search_words.txt

Reads the visible search bodies of one snapshot and segments them with the base
dictionary (jieba's packaged one plus the fixed domain words), never with an
existing word list, so a rerun proposes the same words from the same corpus.

Candidates are runs of 2 to 5 touching words of two kinds:

- name: every touching pair includes a fragment, the same rule the Agent uses to
  join query phrases (阿尔忒弥 + 斯, 中 + 芯 + 国际). At least one single character
  must be rare (dictionary frequency < 5,000) and noun-like in jieba's tags, and
  none may be a verb, adverb or other grammar word, so 很 + 难 or 赚 + 不到 is
  not a name. Personal names (a common surname first, 2 to 4 characters,
  dominance >= 0.5, entropy >= 1.5) skip the tag rule: 许家印, 王文银.
- compound: multi-character words only, where the rarest word occurs inside the
  run in at least 60% of its uses (宁德 + 时代, 营商 + 环境).

Both need 3+ transcripts, varied neighbouring characters on each side (entropy
>= 1.0), and must not usually continue into one longer candidate. Titles before
names (总统尹锡悦), names before ordinary words (星舰项目) and pieces cut at a
word boundary (局副局长) are dropped. BLOCK and ALLOW record the manual review.
Each word gets the smallest jieba frequency that keeps it whole.

Changing the output changes segmentation: bump SEARCH_VERSION and FORMAT_VERSION.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import jieba
import psycopg2
from psycopg2 import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from src.search_query import WORD_CHARACTER_FREQUENCY  # noqa: E402
from src.search_text import BASE_WORDS, STOPWORDS, new_tokenizer  # noqa: E402

NAME_CHARACTER_FREQUENCY = 5000
NOUN_TAGS = {"n", "nr", "ns", "nt", "nz", "ng", "nrt", "nrfg", "j", "vn", "an", "x"}
PART_TAGS = NOUN_TAGS | {"t", "s", "l", "i", "tg"}
# Grammar: verbs, adverbs, prepositions, conjunctions, particles, pronouns,
# numerals, measure words, interjections. Never part of a name.
FUNCTION_TAGS = {"v", "vd", "d", "p", "c", "u", "uj", "ul", "uz", "ug", "uv", "ud"}
FUNCTION_TAGS |= {"r", "rr", "rz", "m", "mq", "q", "y", "e", "o"}
SURNAMES = set(
    "赵魏侯胡徐蔡罗郑郭姜潘巴萨博哈燕宋曹姚陈李王张刘杨黄吴周吕苏马孙朱高林何梁谢韩"
    "唐冯于董程叶袁邓许傅沈彭卢蒋贾丁薛阎余杜戴夏钟汪田任范方石谭廖邹熊金陆郝孔白崔"
    "康毛邱秦江史顾邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文"
)
# A single character that usually ends the previous word: 局 in 某某局副局长.
WORD_END_CHARACTERS = set("局监孩威支波业尔债店企税牌钢氏泰宗诺版将裔姐庭厅商币档芯")
TITLES = {
    "总统", "总理", "首相", "董事长", "教授", "书记", "院长", "市长", "州长", "顾问",
    "右翼", "现在", "今年", "未来", "利用", "此前", "当地", "中国", "全球", "国家",
    "德国总理", "英国首相",
}  # fmt: skip
BLOCK = set(
    "斯坦 埃尔 吉尔 纳尔 艾斯 斯曼 罗夫 索夫 艾尔 尔滨 格伦 将星舰 此前锂 打了个样 "
    "协议班 后备旅 红色旅 媒体课 韦恩同学 本土版 手机版 印度版 中国版 实体版 现实版 "
    "纸质版 山寨版 苏嘉 洪范 穆盟 博卡 欧盟碳 中国碳 全球锂 当地人社局 教授吕德文 "
    "投资收益额 中国货 字面意思 肉眼可见 打发时间 眼睁睁看着 标榜自己 值得商榷 "
    "经不起推敲 没有好下场 一家三口 放下身段 整部电影 学弟学妹 截然相反 令人唏嘘 "
    "进行批评教育 采取有效措施 国与国之间 并非空穴来风 未尝不是 节节攀升 主要负责同志 "
    "全年无休 火力全开 意识淡漠 抗压能力 发短视频 发不出工资 勒紧腰带 睁一只眼闭一只眼 "
    "自证清白 春秋两季 部副部长 读几段".split()
)
BLOCK |= set(
    "久矣 企高管 升成 卡上 卡人 园后 园小区 园方 够大 委原 录下 方面刷 笔钱 狗当 法院判 "
    "高盐高 攒下钱 攒够 硬扛 裁掉 雇人 重判 重燃 频现 畸高 度高 头猪 百人 组数据 节课 "
    "趟车 新晋 学斌 志伟 泽平 宝成 大央 大班 大店 大矿 好货 失焦 失管 车距 媒上 金浩 "
    "刷上 押上 抛下 瞄上 绑上 沾上 踩中 赌上 攒下 脚踩 掺私货 斥巨资 患抑郁症 "
    "专项债中 委书记 委常委 总统齐塞克迪 日本首相岸田文雄 商务部长王文涛 部长唐仁健 "
    "妻子刘结红 创始人黄峥 吴伟仁院士 谷歌前 南孔 戈马 木姐 卫河 蓝州 德士 华德 华诚 "
    "富美 悦城 垣信 宋国 郑国 辽国 汗国 古尔 卡林 皮塔 洛马 沃德 伍德 麦德 马尔 马萨 "
    "阿尔 阿布 阿迪 阿甘 博卡奇卡 菲方 巴方 缅方 樊强 王伟 王勇 王宁 王星 王晓 王莉 "
    "李勇 李宇 李志 李晓 李杰 李磊 李辉 陈勇 陈志 陈慧 陈熙 杨伟 杨慧 刘永 刘慧 朱宏 "
    "韩军 魏新 徐文 吴炜 孙公司 夏商 下午三四点 两把椅子 几户人家 半个身子 必须想个办法 "
    "不过包退 预祝各位 这个节骨眼 华教授 宏教授 燕建议 胡女士 赵女士 潘书记 胡局长 "
    "向世卫组织 和世卫组织 巴国内 货能力 轨铁路 泽同学 媒评论 症患者 奖获得者 "
    "局原副局长 局原局长 美联储主席鲍威尔 宿松县委书记 广州市委书记 团市委书记 "
    "国家安全部部长 记者卡舒吉 首都加拉加斯 首都安卡拉 下划地方 今年晚些时候 "
    "何尝不是一种 不会亏待 保持良好关系 引起强烈反响 伸出援手 想尽一切办法 "
    "提高劳动效率 形象示人 自带干粮 过敏者禁用 靠右行驶 私自阅看 无证据证明 "
    "故意或者重大过失 前款规定 发放职工福利 受到纪律处分 坑外地人".split()
)
ALLOW = set("佩特罗 波罗申科 诺奖 商票 钢企 宗法制".split())
# Place words end a phrase rather than belong to it: 微博上, 车企中, 灾后.
PLACE_ENDINGS = set("上中里前后下内外时间")
LEADING_CHARACTERS = set("上下里前后内外时每此当")


def _tags() -> dict[str, str]:
    tags = {}
    path = Path(jieba.__file__).with_name("dict.txt")
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3:
            tags[parts[0]] = parts[2]
    return tags


def _entropy(counts: Counter) -> float:
    total = sum(counts.values())
    return -sum(v / total * math.log(v / total) for v in counts.values())


def mine(bodies, tokenizer) -> list[dict]:
    def frequency(word: str) -> int:
        return tokenizer.FREQ.get(word, 0)

    def fragment(word: str) -> bool:
        return len(word) == 1 and frequency(word) < WORD_CHARACTER_FREQUENCY

    def kind(words: list[str]) -> str | None:
        if any(
            w in STOPWORDS or w.isascii() or not any(c.isalnum() for c in w)
            for w in words
        ):
            return None
        if all(
            fragment(a) or fragment(b) for a, b in zip(words, words[1:], strict=False)
        ):
            return "name"
        if all(len(w) > 1 for w in words):
            return "compound"
        return None

    tf, df, word_tf, kinds = Counter(), Counter(), Counter(), {}
    left, right = defaultdict(Counter), defaultdict(Counter)
    before, after = defaultdict(Counter), defaultdict(Counter)
    for text in bodies:
        tokens = list(tokenizer.tokenize(text, mode="default", HMM=False))
        word_tf.update(word for word, _, _ in tokens)
        seen = set()
        for i in range(len(tokens)):
            for j in range(i + 2, min(i + 5, len(tokens)) + 1):
                if tokens[j - 1][1] != tokens[j - 2][2]:
                    break
                words = [word for word, _, _ in tokens[i:j]]
                found = kind(words)
                term = "".join(words)
                if found is None or not 2 <= len(term) <= 10:
                    continue
                kinds[term] = found
                tf[term] += 1
                seen.add(term)
                start, end = tokens[i][1], tokens[j - 1][2]
                left[term][text[start - 1] if start else "^"] += 1
                right[term][text[end] if end < len(text) else "$"] += 1
                touching = i and tokens[i - 1][2] == start
                before[term][tokens[i - 1][0] if touching else None] += 1
                touching = j < len(tokens) and tokens[j][1] == end
                after[term][tokens[j][0] if touching else None] += 1
        df.update(seen)

    rows = []
    for term, documents in df.items():
        if documents < 3:
            continue
        words = [w for w, _, _ in tokenizer.tokenize(term, HMM=False)]
        extends = max(
            [
                count / tf[term]
                for word, count in after[term].items()
                if word and kind(words + [word]) and term + word in tf
            ]
            + [
                count / tf[term]
                for word, count in before[term].items()
                if word and kind([word] + words) and word + term in tf
            ]
            + [0]
        )
        rows.append(
            {
                "term": term,
                "kind": kinds[term],
                "parts": words,
                "df": documents,
                "dominance": tf[term] / min(word_tf[w] for w in words),
                "entropy": min(_entropy(left[term]), _entropy(right[term])),
                "extends": extends,
            }
        )
    return rows


def select(rows: list[dict], tokenizer) -> list[str]:
    tags = _tags()

    def frequency(word: str) -> int:
        return tokenizer.FREQ.get(word, 0)

    def person_like(row: dict) -> bool:
        # Chinese personal names use ordinary characters (许家印, 王文银), so the
        # tag rule cannot judge them; a surname start and cohesion can.
        term = row["term"]
        return (
            2 <= len(term) <= 4
            and term[0] in SURNAMES
            and row["dominance"] >= 0.5
            and row["entropy"] >= 1.5
        )

    def name_like(row: dict) -> bool:
        if person_like(row):
            return True
        anchored = False
        for part in row["parts"]:
            tag = tags.get(part, "x")
            if len(part) == 1:
                if tag in FUNCTION_TAGS:
                    return False
                rare = frequency(part) < NAME_CHARACTER_FREQUENCY
                anchored |= rare and tag in NOUN_TAGS | {"zg"}
            elif tag not in PART_TAGS:
                return False
        return anchored and not any(c.isdigit() for c in row["term"])

    candidates = [
        row
        for row in rows
        if row["entropy"] >= 1.0
        and row["extends"] < 0.6
        and (
            (row["kind"] == "name" and name_like(row))
            or (row["kind"] == "compound" and row["dominance"] >= 0.6)
        )
    ]
    terms = {row["term"] for row in candidates}

    def known(word: str) -> bool:
        return frequency(word) > 0 or word in terms

    def dropped(row: dict) -> bool:
        term, parts = row["term"], row["parts"]
        if term in BLOCK:
            return True
        for i in range(1, len(term)):
            head, tail = term[:i], term[i:]
            if tail in terms and head in TITLES:
                return True
            if head in terms and len(head) > 1 and len(tail) > 1 and known(tail):
                return True
        if parts[0] in WORD_END_CHARACTERS and known("".join(parts[1:])):
            return True
        if parts[-1] in PLACE_ENDINGS or parts[0] in LEADING_CHARACTERS:
            return True
        return parts[-1] in SURNAMES and len(parts[-2]) > 1

    kept = {row["term"] for row in candidates if not dropped(row)}
    kept |= ALLOW & {row["term"] for row in rows}
    return sorted(kept)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--schema", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, help="Also save all candidates")
    args = parser.parse_args()
    tokenizer = new_tokenizer(BASE_WORDS)
    with psycopg2.connect(args.dsn) as conn, conn.cursor("bodies") as cursor:
        cursor.itersize = 100
        cursor.execute(
            sql.SQL("SELECT normalized_text FROM {}.search_documents").format(
                sql.Identifier(args.schema)
            )
        )
        rows = mine((text for (text,) in cursor), tokenizer)
    if args.candidates:
        args.candidates.write_text(json.dumps(rows, ensure_ascii=False))
    words = select(rows, tokenizer)
    lines = [
        "# Generated by scripts/discover_search_words.py; reviewed by hand.",
        "# word  smallest jieba frequency that keeps it whole",
    ]
    for word in words:
        lines.append(f"{word} {tokenizer.suggest_freq(word, tune=False)}")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(words)} words written to {args.out}")


if __name__ == "__main__":
    main()
