from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any


WORKFLOW_SODA_MUSIC = "soda_music"
WORKFLOW_REDFRUIT_SHORT_DRAMA = "redfruit_short_drama"
REDFRUIT_DEFAULT_GENRE = ""
REDFRUIT_GENRE_ROOT = "番茄/红果短剧素材题材"
REDFRUIT_GENRE_OTHER = "短剧-其他"
_SODA_WORKFLOW_ALIASES = {
    "soda",
    "sodamusic",
    "qishui",
    "qishuiyinyue",
    "汽水",
    "汽水音乐",
}
_REDFRUIT_WORKFLOW_ALIASES = {
    "redfruit",
    "hongguo",
    "hongguoduanju",
    "redfruitshortdrama",
    "hongguoshortdrama",
    "shortdrama",
    "红果短剧",
    "红果免费短剧",
}


def _normalise_key(value: object) -> str:
    """归一化分类名，兼容空格、短横线和全半角差异。"""
    return re.sub(r"[\s\u00a0_\-－—–/、（）()]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


REDFRUIT_GENRE_GROUPS = {
    "短剧-女频题材": (
        "青春",
        "奇幻爱情",
        "大女主",
        "现言虐恋",
        "萌宝",
        "古风言情",
        "宫斗宅斗",
        "都市情感",
        "民国爱情",
        "家庭伦理",
        "现言甜宠",
        "仙侠情缘",
        "年代爱情",
    ),
    "短剧-男频题材": (
        "赘婿",
        "都市种田",
        "战神",
        "神医",
        "历史古代",
        "都市玄幻",
        "都市日常",
        "玄幻仙侠",
        "都市脑洞",
    ),
    "短剧-通用题材": (
        "抗战谍战",
        "悬疑推理",
        "剧情",
        "志怪",
    ),
}

REDFRUIT_GENRE_ALIASES = {
    "奇幻言情": "奇幻爱情",
    "奇幻爱情": "奇幻爱情",
    "现代虐恋": "现言虐恋",
    "现言虐恋": "现言虐恋",
    "虐恋": "现言虐恋",
    "古风": "古风言情",
    "古装言情": "古风言情",
    "宫斗": "宫斗宅斗",
    "宅斗": "宫斗宅斗",
    "家庭": "家庭伦理",
    "伦理": "家庭伦理",
    "甜宠": "现言甜宠",
    "现言甜宠": "现言甜宠",
    "仙侠情缘": "仙侠情缘",
    "年代": "年代爱情",
    "年代言情": "年代爱情",
    "历史": "历史古代",
    "古代": "历史古代",
    "悬疑": "悬疑推理",
    "推理": "悬疑推理",
    "抗战": "抗战谍战",
    "谍战": "抗战谍战",
}

_REDFRUIT_GENRE_LOOKUP = {
    _normalise_key(leaf): (group, leaf)
    for group, leaves in REDFRUIT_GENRE_GROUPS.items()
    for leaf in leaves
}
_REDFRUIT_GENRE_ALIAS_LOOKUP = {
    _normalise_key(alias): target
    for alias, target in REDFRUIT_GENRE_ALIASES.items()
}

REDFRUIT_DELIVERY_PRODUCTS = ["红果免费短剧(8662)"]
REDFRUIT_DELIVERY_PLATFORMS = [
    "广点通",
    "头条内广",
    "穿山甲联盟",
    "union_app",
    "粉丝通",
    "内广-DPA",
    "UC浏览器",
    "sem",
]
REDFRUIT_ARLP_PRODUCTS = ["红果免费漫剧(8704)", "番茄免费小说(1967)"]
REDFRUIT_ARLP_PLATFORMS = list(REDFRUIT_DELIVERY_PLATFORMS)
REDFRUIT_ARLP_STAGES = [
    {
        "name": "红果漫剧/番茄小说",
        "products": list(REDFRUIT_ARLP_PRODUCTS),
        "platforms": list(REDFRUIT_ARLP_PLATFORMS),
    },
    {
        "name": "短剧端原生IAA",
        "products": ["短剧端原生IAA(796433)"],
        "platforms": ["头条内广"],
    },
    {
        "name": "番茄畅听",
        "products": ["番茄畅听(3040)"],
        "platforms": ["广点通", "头条内广", "穿山甲联盟", "union_app", "sem"],
    },
]
REDFRUIT_FIXED_CUSTOM_TAGS = [
    "dxzc",
    "漫剧",
    "未成年人已授权",
    "影视版权已授权",
    "DX6",
    "dxcz-番茄测试",
    "AI文生视频，无真人肖像输入",
]
REDFRUIT_LIVE_ACTION_FIXED_CUSTOM_TAGS = [
    "dxzc",
    "短剧洞察",
    "生产赋能专项",
    "DX6",
    "dxcz-番茄测试",
    "未成年人已授权",
    "影视版权已授权",
]
REDFRUIT_REAL_PERSON_TITLE_LABELS = (
    "仿真人动态解说剧",
    "仿真人剧",
)
REDFRUIT_DYNAMIC_TITLE_LABELS = (
    "2D动态解说剧",
    "3D动态解说剧",
    "3D动画漫剧",
    "2D动画漫剧",
    "表情包漫剧",
    "逆水寒漫剧",
    "静态解说漫剧",
)
REDFRUIT_PLAYLET_URL = "https://usergrowth.com.cn/aigc/insight/business/playlet?source=13"
REDFRUIT_CONTENT_KINDS = ("动态漫", "仿真人", "纯短剧")


def redfruit_content_kind(value: object) -> str:
    """把文件名、工单名或短剧选剧剧名标签归一到三种红果剧目类型。"""
    compact = _normalise_key(value)
    if not compact:
        return ""
    if "仿真人" in compact or any(_normalise_key(label) in compact for label in REDFRUIT_REAL_PERSON_TITLE_LABELS):
        return "仿真人"
    if "纯短剧" in compact or "真人实拍" in compact or "真人剧" in compact:
        return "纯短剧"
    if (
        "动态漫" in compact
        or "动画漫剧" in compact
        or "动态解说剧" in compact
        or any(_normalise_key(label) in compact for label in REDFRUIT_DYNAMIC_TITLE_LABELS)
    ):
        return "动态漫"
    return ""


def require_redfruit_content_kind(value: object, *, source: str = "红果剧目类型") -> str:
    """归一化并校验红果剧目类型，防止未知类型静默落到动态漫分支。"""
    drama_type = redfruit_content_kind(value)
    if drama_type:
        return drama_type
    raise ValueError(
        f"{source}未识别到明确类型，必须包含动态漫、仿真人或纯短剧（真人剧/真人实拍短剧）之一。"
    )


def redfruit_order_kind(value: object) -> str:
    """把红果工单名归一为动态漫/仿真人/纯短剧。"""
    return redfruit_content_kind(value)


def redfruit_expected_order_kind(file_name: object, *, drama_type: object = "", material_mode: object = "") -> str:
    """根据文件名上的剧目类型判断应使用哪类工单；素材类型不参与前置工单校验。"""
    _ = material_mode
    return redfruit_content_kind(drama_type or file_name)


def redfruit_extract_order_title(body_text: str, order_id: str) -> str:
    """从工单搜索结果正文中提取当前订单标题。"""
    wanted = str(order_id or "").strip()
    if not wanted:
        return ""
    lines = [line.strip() for line in re.split(r"\r?\n", str(body_text or "")) if line.strip()]
    id_pattern = re.compile(rf"^ID\s*[:：]\s*{re.escape(wanted)}\s*$", flags=re.IGNORECASE)
    for index, line in enumerate(lines):
        if not id_pattern.search(line):
            continue
        for prev_index in range(index - 1, -1, -1):
            candidate = lines[prev_index].strip()
            if candidate and candidate not in {"订单名称", "订单", "操作"}:
                return candidate
    match = re.search(rf"([^\n\r]+)\s*\n\s*ID\s*[:：]\s*{re.escape(wanted)}\b", str(body_text or ""), flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def redfruit_extract_playlet_card(body_text: str, drama_title: str) -> dict[str, str]:
    """从短剧选剧搜索结果正文中提取剧名标签、题材分类和 BID。"""
    title = str(drama_title or "").strip()
    title_key = _normalise_key(title)
    lines = [line.strip() for line in re.split(r"\r?\n", str(body_text or "")) if line.strip()]
    for index, line in enumerate(lines):
        line_key = _normalise_key(line)
        if not title_key or not (line_key == title_key or title_key in line_key):
            continue
        label_line = ""
        genre = ""
        for next_index in range(index + 1, min(len(lines), index + 18)):
            candidate = lines[next_index].strip()
            if not candidate:
                continue
            if candidate.startswith(("分类：", "分类:")):
                if candidate in {"分类：", "分类:"} and next_index + 1 < len(lines):
                    genre = lines[next_index + 1].strip()
                else:
                    genre = re.sub(r"^分类[:：]\s*", "", candidate).strip()
                break
            if "/" in candidate and not label_line:
                label_line = candidate
        bid = redfruit_extract_bid(str(body_text or ""))
        return {
            "found": "true",
            "title": line,
            "label_line": label_line,
            "title_label": _playlet_title_label_from_line(label_line),
            "title_kind": redfruit_content_kind(label_line),
            "genre": genre,
            "bid": bid,
        }
    return {
        "found": "false",
        "title": "",
        "label_line": "",
        "title_label": "",
        "title_kind": "",
        "genre": "",
        "bid": redfruit_extract_bid(str(body_text or "")),
    }


def redfruit_extract_bid(text: str) -> str:
    """从页面正文或文件名片段中提取红果 BID 并统一成 bid_数字。"""
    match = re.search(r"(?i)\bBID\b\s*[:：]\s*(\d{8,})", str(text or ""))
    if match:
        return f"bid_{match.group(1)}"
    return redfruit_bid(str(text or ""))


def redfruit_format_preflight_failure(errors: list[str], warnings: list[str] | None = None) -> str:
    """把红果前置校验结果格式化成醒目的阻断信息。"""
    lines = [
        "**红果短剧前置校验失败！！**",
        "**已停止后续上传流程！！请先核对工单、文件名分类和 BID！！**",
    ]
    for message in errors:
        lines.append(f"!! {message}")
    for message in warnings or []:
        lines.append(f"! {message}")
    return "\n".join(lines)


def _playlet_title_label_from_line(label_line: str) -> str:
    if not label_line:
        return ""
    parts = [part.strip() for part in label_line.split("/") if part.strip()]
    return parts[-1] if parts else ""


def normalise_workflow(value: object) -> str:
    text = _normalise_token(value)
    if text in _REDFRUIT_WORKFLOW_ALIASES:
        return WORKFLOW_REDFRUIT_SHORT_DRAMA
    return WORKFLOW_SODA_MUSIC


def require_upload_workflow(value: object) -> str:
    """解析上传 CLI 的明确工作流，拒绝未知值而不是静默回退到汽水。"""
    text = _normalise_token(value)
    if not text or text in _SODA_WORKFLOW_ALIASES:
        return WORKFLOW_SODA_MUSIC
    if text in _REDFRUIT_WORKFLOW_ALIASES:
        return WORKFLOW_REDFRUIT_SHORT_DRAMA
    raise ValueError(
        f"不支持的上传 workflow={value!r}。usergrowth_upload.py 仅支持 soda_music 或 "
        "redfruit_short_drama；番茄音乐 CID/BID 打标请使用 tomato_music_tagging.py。"
    )


def is_redfruit_workflow(value: object) -> bool:
    return normalise_workflow(value) == WORKFLOW_REDFRUIT_SHORT_DRAMA


def default_delivery_products(workflow: object) -> list[str]:
    if is_redfruit_workflow(workflow):
        return list(REDFRUIT_DELIVERY_PRODUCTS)
    return ["汽水音乐"]


def default_delivery_platforms(workflow: object) -> list[str]:
    if is_redfruit_workflow(workflow):
        return list(REDFRUIT_DELIVERY_PLATFORMS)
    return []


def default_delivery_platform_all(workflow: object) -> bool:
    return not is_redfruit_workflow(workflow)


def default_arlp_products(workflow: object) -> list[str]:
    if is_redfruit_workflow(workflow):
        return list(REDFRUIT_ARLP_PRODUCTS)
    return []


def default_arlp_platforms(workflow: object) -> list[str]:
    if is_redfruit_workflow(workflow):
        return list(REDFRUIT_ARLP_PLATFORMS)
    return []


def default_arlp_platform_all(workflow: object) -> bool:
    return False


def default_arlp_stages(workflow: object) -> list[dict[str, Any]]:
    if not is_redfruit_workflow(workflow):
        return []
    return [
        {
            "name": str(stage["name"]),
            "products": list(stage["products"]),
            "platforms": list(stage["platforms"]),
        }
        for stage in REDFRUIT_ARLP_STAGES
    ]


def build_redfruit_metadata(
        path: Path,
        *,
        default_genre: str = REDFRUIT_DEFAULT_GENRE,
        bid_map: dict[str, Any] | None = None,
        layout_override: str = "",
        material_mode_override: str = "",
        ai_custom_tag: str = "创意AI素材",
        extra_custom_tags: list[str] | None = None,
) -> dict[str, Any]:
    file_name = path.name
    stem = Path(file_name).stem
    drama_type = require_redfruit_content_kind(file_name, source=f"文件【{file_name}】")
    material_mode = _normalise_redfruit_material_mode_override(material_mode_override) or redfruit_material_mode(file_name)
    title = redfruit_drama_title(file_name)
    bid = redfruit_bid(file_name) or _lookup_redfruit_bid(title, file_name, bid_map)
    layout = _normalise_redfruit_layout_override(layout_override) or redfruit_layout_label(path)
    genre_path = redfruit_genre_path(file_name, default_genre=default_genre)
    genre = genre_path[-1] if genre_path else REDFRUIT_GENRE_OTHER
    genre_group = genre_path[1] if len(genre_path) > 2 else REDFRUIT_GENRE_OTHER
    has_title_label = "无剧名素材" if "无剧名" in stem else "原剧名素材"
    material_type_path = redfruit_material_type_path(material_mode)
    classification_paths = [
        ["番茄/红果小说素材版式", "视频版式", layout],
        redfruit_feature_path(drama_type, material_mode),
        material_type_path,
        ["IOS/非IOS", "短剧通用素材"],
        ["尺度素材", "无尺度"],
        genre_path,
        ["有无短剧剧名", has_title_label],
        ["小说/短剧审核分流", "【测试】无logo纯短剧"],
    ]
    post_review_classification_paths = redfruit_post_review_classification_paths(drama_type)
    custom_tags = redfruit_custom_tags(
        file_name,
        bid=bid,
        material_mode=material_mode,
        ai_custom_tag=ai_custom_tag,
        extra_tags=extra_custom_tags,
    )
    return {
        "workflow": WORKFLOW_REDFRUIT_SHORT_DRAMA,
        "drama_title": title,
        "drama_type": drama_type,
        "material_mode": material_mode,
        "bid": bid,
        "layout": layout,
        "genre": genre,
        "genre_group": genre_group,
        "genre_path": genre_path,
        "has_drama_title": has_title_label,
        "classification_paths": classification_paths,
        "post_review_classification_paths": post_review_classification_paths,
        "custom_tags": custom_tags,
        "delivery_products": list(REDFRUIT_DELIVERY_PRODUCTS),
        "delivery_platforms": list(REDFRUIT_DELIVERY_PLATFORMS),
        "delivery_platform_all": False,
        "arlp_products": list(REDFRUIT_ARLP_PRODUCTS),
        "arlp_platforms": list(REDFRUIT_ARLP_PLATFORMS),
        "arlp_platform_all": False,
        "arlp_stages": default_arlp_stages(WORKFLOW_REDFRUIT_SHORT_DRAMA),
        "warnings": redfruit_warnings(title=title, bid=bid, genre_path=genre_path),
    }


def redfruit_warnings(*, title: str, bid: str, genre_path: list[str] | None = None) -> list[str]:
    warnings: list[str] = []
    if not title:
        warnings.append("文件名未识别到剧名")
    if not bid:
        warnings.append("文件名未识别到 BID，自定义标签未追加 bid_剧目ID")
    if not genre_path or genre_path[-1] == REDFRUIT_GENRE_OTHER:
        warnings.append("短剧题材未命中平台标签，已按短剧-其他打标")
    return warnings


def redfruit_drama_type(file_name: str) -> str:
    return redfruit_content_kind(file_name)


def redfruit_drama_title(file_name: str) -> str:
    parts = _split_name_parts_preserving_punctuation(file_name)
    if len(parts) >= 3 and _looks_like_redfruit_type(parts[1]):
        return parts[2].strip()
    for index, part in enumerate(parts):
        if _looks_like_redfruit_type(part) and index + 1 < len(parts):
            return parts[index + 1].strip()
    return ""


def redfruit_bid(file_name: str) -> str:
    text = unicodedata.normalize("NFKC", str(file_name or ""))
    patterns = (
        r"(?i)bid[\s_-]*(\d{8,})",
        r"(?i)b[\s_-]*id[\s_-]*(\d{8,})",
        r"剧目(?:id|ID)[\s_：:-]*(\d{8,})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return f"bid_{match.group(1)}"
    return ""


def redfruit_material_mode(file_name: str) -> str:
    text = _compact(file_name)
    if "ai前贴" in text or "aigc前贴" in text or "原创ai" in text or "原创aigc" in text:
        return "AI前贴"
    if "ai后贴" in text or "aigc后贴" in text:
        return "AI后贴"
    if "功能综述" in text or "功能总述" in text:
        return "功能综述"
    if "解说" in text or "旁白" in text:
        return "解说"
    if "bgm混剪" in text or "音乐混剪" in text:
        return "BGM混剪"
    if "混剪" in text:
        return "混剪"
    return "原片"


def redfruit_layout_label(path: Path) -> str:
    text = _compact(path.name)
    if "横改竖" in text:
        return "竖版-横改竖"
    if "竖改横" in text:
        return "横版-竖改横"
    if "纯横" in text:
        return "横版-纯横版"
    if "纯竖" in text:
        return "竖版-纯竖版"
    if "横版" in text or "横板" in text:
        return "横版-纯横版"
    if "竖版" in text or "竖板" in text:
        return "竖版-纯竖版"

    width, height = _probe_video_dimensions(path)
    if width and height:
        return "横版-纯横版" if width >= height else "竖版-纯竖版"
    return "竖版-纯竖版"


def redfruit_feature_group(drama_type: str) -> str:
    normalized_type = require_redfruit_content_kind(drama_type)
    if normalized_type == "纯短剧":
        raise ValueError("纯短剧使用独立的短剧功能卖点路径，不使用漫剧功能分组。")
    return "仿真人剧" if normalized_type == "仿真人" else "动态漫"


def redfruit_feature_leaf(drama_type: str, material_mode: str) -> str:
    normalized_type = require_redfruit_content_kind(drama_type)
    if normalized_type == "纯短剧":
        raise ValueError("纯短剧使用固定的短剧功能卖点路径，不使用漫剧功能叶子。")
    if material_mode == "功能综述":
        return "仿真功能综述" if normalized_type == "仿真人" else "动态漫功能综述"
    if material_mode in {"混剪", "BGM混剪"}:
        return "剧情混剪" if normalized_type == "仿真人" else "BGM混剪"
    if material_mode == "解说":
        return "旁白解说" if normalized_type == "仿真人" else "纯功能介绍"
    return "原片剪辑"


def redfruit_feature_path(drama_type: str, material_mode: str) -> list[str]:
    normalized_type = require_redfruit_content_kind(drama_type)
    if normalized_type == "纯短剧":
        return ["番茄/红果小说功能卖点", "短剧", "纯短剧内容", "纯短剧"]
    return [
        "番茄/红果小说功能卖点",
        redfruit_feature_group(normalized_type),
        redfruit_feature_leaf(normalized_type, material_mode),
    ]


def redfruit_post_review_classification_paths(drama_type: str) -> list[list[str]]:
    normalized_type = require_redfruit_content_kind(drama_type)
    ai_label = "非AI素材" if normalized_type == "纯短剧" else "AI素材"
    content_form = "短剧" if normalized_type == "纯短剧" else "漫剧"
    if normalized_type == "纯短剧":
        feature_path = ["番茄畅听功能卖点", "短剧", "短剧内容", "红果同步短剧", "纯短剧内容"]
    elif normalized_type == "仿真人":
        feature_path = ["番茄畅听功能卖点", "仿真人短剧", "仿真人内容", "红果同步仿真人"]
    else:
        feature_path = ["番茄畅听功能卖点", "动态漫", "漫剧内容", "红果同步漫剧", "漫剧剪辑"]
    return [
        ["番茄畅听素材类型", "剪辑制作", "常规剪辑"],
        ["番茄畅听IOS/非IOS", "通用素材"],
        ["有无logo", "无logo以及其他的产品信息"],
        ["自动过审", "自动过审"],
        ["是否为AI素材", ai_label],
        ["免费短剧-素材剪辑形式", "原片剪辑"],
        feature_path,
        ["是否带免费利益点", "是"],
        ["有无歌曲名露出(非音乐类素材不要打)", "非歌曲方向素材"],
        ["小程序系产品-内容体裁", content_form],
    ]


def redfruit_material_type_path(material_mode: str) -> list[str]:
    if material_mode in {"AI前贴", "AI后贴"}:
        return ["番茄/红果小说素材类型", "信息流素材类型", "AI素材", "AI前贴/后贴"]
    return ["番茄/红果小说素材类型", "信息流素材类型", "纯原片剪辑"]


def redfruit_genre(file_name: str, *, default_genre: str = REDFRUIT_DEFAULT_GENRE) -> str:
    """返回短剧题材叶子；未命中平台可选标签时返回短剧-其他。"""
    return redfruit_genre_path(file_name, default_genre=default_genre)[-1]


def redfruit_genre_path(file_name: str, *, default_genre: str = REDFRUIT_DEFAULT_GENRE) -> list[str]:
    """把墨攻选剧灵感题材对齐到 UserGrowth 分类标签路径。"""
    for source in (file_name, default_genre):
        path = _match_redfruit_genre_path(source)
        if path:
            return path
    return [REDFRUIT_GENRE_ROOT, REDFRUIT_GENRE_OTHER]


def _match_redfruit_genre_path(value: object) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    if not text:
        return []
    compact = _normalise_key(text)
    if "短剧其他" in compact or compact in {"其他", "短剧其它", "其它"}:
        return [REDFRUIT_GENRE_ROOT, REDFRUIT_GENRE_OTHER]

    for leaf_key, (group, leaf) in _REDFRUIT_GENRE_LOOKUP.items():
        if leaf_key and leaf_key in compact:
            return [REDFRUIT_GENRE_ROOT, group, leaf]

    for alias_key, target_leaf in _REDFRUIT_GENRE_ALIAS_LOOKUP.items():
        if alias_key and alias_key in compact:
            matched = _REDFRUIT_GENRE_LOOKUP.get(_normalise_key(target_leaf))
            if matched:
                group, leaf = matched
                return [REDFRUIT_GENRE_ROOT, group, leaf]
    return []


def redfruit_custom_tags(
        file_name: str,
        *,
        bid: str = "",
        material_mode: str | None = None,
        ai_custom_tag: str = "创意AI素材",
        extra_tags: list[str] | None = None,
) -> list[str]:
    drama_type = redfruit_drama_type(file_name)
    tags = list(
        REDFRUIT_LIVE_ACTION_FIXED_CUSTOM_TAGS
        if drama_type == "纯短剧"
        else REDFRUIT_FIXED_CUSTOM_TAGS
    )
    bid = bid or redfruit_bid(file_name)
    if bid:
        tags.append(bid)
    if drama_type:
        tags.append(drama_type)
    tags.extend(redfruit_editor_tags(file_name))
    mode = material_mode or redfruit_material_mode(file_name)
    if mode == "AI前贴" and str(ai_custom_tag or "").strip():
        tags.append(str(ai_custom_tag).strip())
    tags.extend(str(tag).strip() for tag in (extra_tags or []) if str(tag).strip())
    return _dedupe_tags(tags)


def _normalise_redfruit_material_mode_override(value: object) -> str:
    text = _compact(value)
    if not text:
        return ""
    if "ai前/后贴" in text or "ai前后贴" in text or "ai素材" in text:
        return "AI前贴"
    if "ai前贴" in text or "aigc前贴" in text or "原创ai" in text:
        return "AI前贴"
    if "ai后贴" in text or "aigc后贴" in text:
        return "AI后贴"
    if "功能综述" in text or "功能总述" in text:
        return "功能综述"
    if "解说" in text or "旁白" in text:
        return "解说"
    if "bgm混剪" in text or "音乐混剪" in text:
        return "BGM混剪"
    if "混剪" in text:
        return "混剪"
    if "原片" in text:
        return "原片"
    return ""


def _normalise_redfruit_layout_override(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = _compact(text)
    if "横改竖" in compact:
        return "竖版-横改竖"
    if "竖改横" in compact:
        return "横版-竖改横"
    if "纯横" in compact:
        return "横版-纯横版"
    if "纯竖" in compact:
        return "竖版-纯竖版"
    if "横版" in compact or "横板" in compact:
        return "横版-纯横版"
    if "竖版" in compact or "竖板" in compact:
        return "竖版-纯竖版"
    return text.replace("竖板", "竖版").replace("横板", "横版")


def redfruit_editor_tags(file_name: str) -> list[str]:
    parts = _split_name_parts(file_name)
    ignored = {
        "dxzc",
        "动态漫",
        "仿真人",
        "纯短剧",
        "真人剧",
        "真人实拍短剧",
        "动态漫/仿真人",
        "有剧名",
        "无剧名",
        "原剧名",
        "六部",
        "原片",
        "解说",
        "混剪",
        "功能综述",
        "原创AI前贴",
        "原创AI",
        "AI前贴",
        "AI后贴",
    }
    tags: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or text in ignored:
            continue
        if re.fullmatch(r"\d{3,8}", text):
            continue
        if re.fullmatch(r"(?i)bid_?\d+", text):
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,8}", text):
            tags.append(text)
    return tags


def _split_name_parts(file_name: str) -> list[str]:
    stem = Path(str(file_name or "")).stem
    stem = unicodedata.normalize("NFKC", stem)
    stem = stem.replace("—", "-").replace("–", "-").replace("－", "-")
    return [part.strip() for part in re.split(r"\s*-\s*", stem) if part.strip()]


def _split_name_parts_preserving_punctuation(file_name: str) -> list[str]:
    """拆分红果文件名时保留剧名原始标点，避免中文逗号被 NFKC 改成英文逗号。"""
    stem = Path(str(file_name or "")).stem
    stem = stem.replace("—", "-").replace("–", "-").replace("－", "-")
    return [part.strip() for part in re.split(r"\s*-\s*", stem) if part.strip()]


def _looks_like_redfruit_type(value: str) -> bool:
    return any(token in value for token in ("动态漫", "仿真人", "漫剧", "纯短剧", "真人实拍", "真人剧"))


def _compact(value: object) -> str:
    return re.sub(r"[\s\u00a0_]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def _normalise_token(value: object) -> str:
    return re.sub(r"[\s_\-]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def redfruit_batch_signature(
        file_name: str,
        *,
        default_genre: str = REDFRUIT_DEFAULT_GENRE,
        bid_map: dict[str, Any] | None = None,
        layout_override: str = "",
        material_mode_override: str = "",
        ai_custom_tag: str = "创意AI素材",
        extra_custom_tags: list[str] | None = None,
) -> str:
    """把单个文件压成可用于自动拆批的稳定签名。"""
    meta = build_redfruit_metadata(
        Path(file_name),
        default_genre=default_genre,
        bid_map=bid_map,
        layout_override=layout_override,
        material_mode_override=material_mode_override,
        ai_custom_tag=ai_custom_tag,
        extra_custom_tags=extra_custom_tags,
    )
    parts = [
        meta.get("drama_title", ""),
        meta.get("drama_type", ""),
        meta.get("material_mode", ""),
        meta.get("bid", ""),
        meta.get("layout", ""),
        " > ".join(meta.get("genre_path", [])),
        " / ".join(" > ".join(path) for path in meta.get("classification_paths", [])),
        " / ".join(" > ".join(path) for path in meta.get("post_review_classification_paths", [])),
        "、".join(meta.get("custom_tags", [])),
        json.dumps(meta.get("arlp_stages", []), ensure_ascii=False, sort_keys=True),
    ]
    return "||".join(str(part or "").strip() for part in parts)


def redfruit_batch_label(
        file_name: str,
        *,
        default_genre: str = REDFRUIT_DEFAULT_GENRE,
        bid_map: dict[str, Any] | None = None,
        layout_override: str = "",
        material_mode_override: str = "",
        ai_custom_tag: str = "创意AI素材",
        extra_custom_tags: list[str] | None = None,
) -> str:
    """生成红果自动拆批的显示名称。"""
    meta = build_redfruit_metadata(
        Path(file_name),
        default_genre=default_genre,
        bid_map=bid_map,
        layout_override=layout_override,
        material_mode_override=material_mode_override,
        ai_custom_tag=ai_custom_tag,
        extra_custom_tags=extra_custom_tags,
    )
    title = meta.get("drama_title") or Path(file_name).stem
    mode = meta.get("material_mode") or "原片"
    bid = meta.get("bid") or "未识别bid"
    return f"{title}-{mode}-{bid}"


def _lookup_redfruit_bid(title: str, file_name: str, bid_map: dict[str, Any] | None) -> str:
    if not bid_map:
        return ""
    candidates = [title, Path(file_name).stem, file_name]
    normalized_candidates = [_normalise_token(candidate) for candidate in candidates if str(candidate or "").strip()]
    for key, value in bid_map.items():
        normalized_key = _normalise_token(key)
        if not normalized_key:
            continue
        if any(normalized_key == candidate or normalized_key in candidate for candidate in normalized_candidates):
            bid = _normalise_redfruit_bid_value(value)
            if bid:
                return bid
    return ""


def _normalise_redfruit_bid_value(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "").strip())
    if not text:
        return ""
    match = re.search(r"(?i)bid[\s_-]*(\d{8,})", text)
    if match:
        return f"bid_{match.group(1)}"
    match = re.search(r"(\d{8,})", text)
    if match:
        return f"bid_{match.group(1)}"
    return text


def _probe_video_dimensions(path: Path) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size <= 0:
        return 0, 0
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception:
        return 0, 0
    if proc.returncode != 0:
        return 0, 0
    try:
        payload = json.loads(proc.stdout or "{}")
        stream = next(iter(payload.get("streams") or []), {})
        return int(stream.get("width") or 0), int(stream.get("height") or 0)
    except Exception:
        return 0, 0


def _dedupe_tags(tags: list[str]) -> list[str]:
    deduped: list[str] = []
    for tag in tags:
        text = str(tag or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped
