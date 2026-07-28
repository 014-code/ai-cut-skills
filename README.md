# AI Cut Skills

面向 Codex 与 WorkBuddy 的视频生产 Skill 集合。仓库按能力层级做逻辑分类，但所有 Skill 仍保持 `skills/<skill-name>` 的扁平目录，确保运行时可以直接发现。

分类、依赖和同步范围以根目录的 [`skill-catalog.yaml`](skill-catalog.yaml) 为准。

## Skill 分类

### 1. 运行环境

- `setup-video-editing-environment`：跨平台发现、复用、安装和检查 Python、FFmpeg/FFprobe、Whisper，以及可选的 Node、Chrome 和 Remotion 依赖。

### 2. 素材获取与治理

- `douyin-video-toolkit`：抖音页面捕获、视频流采集、URL/GID/关键词批量下载和失败诊断。
- `mogong-gid-retrieval`：抖音 URL/GID/关键词解析、万邦搜索、魔工 GID 能力查询、匹配结果导出和可选下载。
- `manage-visual-asset-library`：跨项目图片/视频入库、Read 内容理解、有效区域标注、Manifest 校验和语义候选报告。

`douyin-video-toolkit` 负责通用素材解析与下载；`mogong-gid-retrieval` 负责魔工业务查询、过滤和结果导出。后续重构应优先让魔工流程复用通用下载能力，避免继续复制短链解析、GID 提取和万邦下载代码。

### 3. 通用渲染组件

- `subtitle-motion-effects`：Remotion 字幕动效层渲染，支持透明字幕层、合成预览和多字重字体目录。
- `video-motion-effects`：Remotion 图片入场动效，可输出合成视频或透明 ProRes 4444 动效层。

### 4. 业务成片工作流

- `aivideoeditor-pre-roll`：独立本地前贴视频渲染，覆盖资产清单、Logo 选择、字幕模式、免责声明和预检，不依赖远程服务。
- `edit-soda-music-video`：汽水音乐竖屏数字人口播混剪，覆盖素材理解、去气口、Whisper 字幕、BGM、合规、品牌布局、导出和正式交付 QA。
- `edit-short-drama-packaging`：短剧轻包装，覆盖免费利益点、风险提示、AI 生成提示、原尾板替换和横竖屏尾板拼接。

### 5. 衍生加工

- `aivideoeditor-video-fission`：本地视频裂变与素材重混，支持抽帧变体、前贴排列组合、文件夹组合和音视频配对输出。

### 6. 分发自动化

- `aivideoeditor-usergrowth-automation`：UserGrowth 桌面自动上传，支持歌曲库匹配、Excel/CID 回填、素材标签、送审和诊断产物。

## 能力链路

```text
运行环境
   ↓
素材获取 → 素材理解与 Manifest
   ↓
通用渲染组件 → 业务成片
   ↓
视频裂变
   ↓
上传与分发
```

关键依赖：

| 调用方 | 必需能力 | 可选能力 | 推荐下一阶段 |
| --- | --- | --- | --- |
| `edit-soda-music-video` | `setup-video-editing-environment`、`manage-visual-asset-library` | `video-motion-effects` | `aivideoeditor-video-fission` |
| `aivideoeditor-pre-roll` | 无 | `manage-visual-asset-library`、`subtitle-motion-effects` | `aivideoeditor-video-fission` |
| `edit-short-drama-packaging` | 无 | `setup-video-editing-environment` | `aivideoeditor-video-fission` |
| `aivideoeditor-video-fission` | 无 | `setup-video-editing-environment` | `aivideoeditor-usergrowth-automation` |
| `mogong-gid-retrieval` | 无 | `douyin-video-toolkit` | `manage-visual-asset-library` |

完整的机器可读关系见 [`skill-catalog.yaml`](skill-catalog.yaml)。

## 目录

```text
.
├── skill-catalog.yaml
├── scripts/
│   └── sync_skills.py
└── skills/
    ├── setup-video-editing-environment/
    ├── douyin-video-toolkit/
    ├── mogong-gid-retrieval/
    ├── manage-visual-asset-library/
    ├── subtitle-motion-effects/
    ├── video-motion-effects/
    ├── aivideoeditor-pre-roll/
    ├── edit-soda-music-video/
    ├── edit-short-drama-packaging/
    ├── aivideoeditor-video-fission/
    └── aivideoeditor-usergrowth-automation/
```

## 安装与同步

仓库中的 `skills/` 是唯一可信源。不要只修改 `~/.codex/skills` 或 `~/.workbuddy/skills` 中的运行副本。

克隆仓库后，先检查分类清单：

```bash
git clone git@github.com:liudu2326526/ai-cut-skills.git
cd ai-cut-skills
python3 scripts/sync_skills.py --check
python3 scripts/sync_skills.py --list
```

同步全部 Skill 到 Codex 和 WorkBuddy：

```bash
python3 scripts/sync_skills.py --runtime all
```

只同步一个运行时、分类或 Skill：

```bash
python3 scripts/sync_skills.py --runtime codex
python3 scripts/sync_skills.py --runtime codex --category production
python3 scripts/sync_skills.py --runtime workbuddy --skill edit-soda-music-video
```

先预览操作：

```bash
python3 scripts/sync_skills.py --runtime all --dry-run
```

默认运行目录：

- Codex：`${CODEX_HOME:-$HOME/.codex}/skills`
- WorkBuddy：`${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills`

可以通过 `--codex-skills-dir` 和 `--workbuddy-skills-dir` 覆盖。同步默认删除目标 Skill 内已经不在仓库中的旧文件，但会保留 `node_modules`、`__pycache__`、`.npm`、缓存目录和编译产物；需要保留所有旧文件时使用 `--no-delete`。

首次使用 Remotion 动效时，在实际运行目录安装锁定依赖：

```bash
node "${CODEX_HOME:-$HOME/.codex}/skills/video-motion-effects/scripts/remotion/render.mjs" setup
node "${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills/video-motion-effects/scripts/remotion/render.mjs" setup
node "${CODEX_HOME:-$HOME/.codex}/skills/subtitle-motion-effects/scripts/remotion/render.mjs" setup
node "${WORKBUDDY_HOME:-$HOME/.workbuddy}/skills/subtitle-motion-effects/scripts/remotion/render.mjs" setup
```

各 Skill 的输入、输出和门禁规则请查看对应目录下的 `SKILL.md`。
