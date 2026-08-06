# Keyword Text and Audio Redaction

Use this reference for business-rule subtitle masking and synchronized audio muting simulation.

## Scope

This rule set covers:

- subtitle masking for every keyword hit
- audio muting for every subtitle-masked hit
- optional preview rendering of original vs processed subtitle lines
- repeated occurrences of the same keyword must each be masked when they appear at different spans

## Keyword Groups

### 色情低俗

Trigger subtitle masking for terms such as:

`一夜`, `婚外`, `偷情`, `一夜七次`, `第一次`, `一夜激情`, `堕胎`, `打胎`, `流产`, `陪睡`, `出轨`, `借种`, `失身`, `把持不住`, `酒醉失身`, `偷种`, `一夜怀上`, `强行受孕`, `自杀`, `自尽`, `殉情`, `舔狗`, `小三`, `童养媳`, `娃娃亲`

These terms are still subtitle hits, and the current runtime mutes their spans as well because every subtitle hit triggers audio mute:

`你妈逼`, `妈的`, `特么`, `他妈的`, `我靠`, `草泥马`, `玛德`, `妈蛋`, `碧池`, `碧莲`, `装 13`, `装13`, `卧槽`, `我擦`, `我草`, `尼玛`, `滚粗`, `叼你老母`, `浸猪笼`, `傻逼`, `王八蛋`, `畜生`, `杂种`, `tmd`, `nmd`, `rnm`, `sb`, `屌丝`, `土鳖`, `狗东西`, `贱货`, `小婊砸`, `绿茶婊`, `仙人板板`, `龟儿子`, `哈批`, `哈儿`

### 封建迷信

Mask terms such as:

`改命`, `改运`, `借命`, `借运`, `算卦`, `气运`, `冥婚`, `冲喜`, `公鸡拜堂`, `配阴婚`, `阴婚`, `法轮功`, `驱鬼`, `招魂`, `血光之灾`, `消灾`, `辟邪`, `蛇胎`, `鬼魂索命`, `亡灵`, `风水`, `占卜`, `旺运`, `招财消灾`, `起死回生`

### 涉军涉政

Mask terms such as:

`警察`, `警察局`, `派出所`, `民政局`, `团长`, `营长`, `旅长`, `首长`, `解放军`, `中国`, `中华人民共和国`, `一国两制`, `台独`, `港独`, `疆独`, `藏独`, `一中一台`, `两个中国`, `军委`, `中央`, `部委`, `省委`, `市委`, `政府机关`, `特供`, `专供`, `国宴专用`, `RMDHT`, `GYZY`, `日本`, `小日本`, `日军`, `抗日`, `台湾`, `香港`, `澳门`, `美国`, `韩国`, `习近平`, `习大大`, `毛泽东`, `毛爷爷`, `毛主席`, `周恩来`, `邓小平`, `胡锦涛`, `袁世凯`

### 竞品及私域导流

Mask terms such as:

`微信`, `vx`, `QQ`, `支付宝`, `快手`, `红果`, `番茄小说`, `河马`, `抖音`, `小红书`, `腾讯`, `爱奇艺`, `优酷`, `芒果`

## Inputs

Provide a transcript JSON with a list of timestamped segments:

```json
[
  {"start_time": 0.5, "end_time": 2.0, "text": "今晚他第一次向她表白"},
  {"start_time": 2.2, "end_time": 3.7, "text": "我靠，你别再提微信和QQ了"}
]
```

The tool will:

- mask every matched keyword in `masked_text`
- add `audio_mute` to every hit
- keep mute spans aligned to the segment when no finer token timestamps are available
- if `words`, `tokens`, or `frontend.words` exist, resolve hits to word-level spans first
- match keywords after normalizing spaces and punctuation, so variants like `装 13`, `：tmd`, `vx`, `QQ`, and OCR text with mixed punctuation still hit their canonical terms
- preserve repeated hits by span; do not collapse them only because the keyword text repeats
- keep a small pre-roll on subtitle masking so the first visible subtitle frame is covered immediately
- for source-video rendering, scan the entire video with RapidOCR at a fixed interval; do not restrict OCR to ASR-hit segments
- merge adjacent OCR detections for the same keyword into a continuous window with a short hold to prevent temporal gaps
- use only the OCR line box narrowed to the matched character span; never use the full frame, whole person, or full subtitle band to compensate for a missed OCR location

## Entrypoint

Prefer:

```powershell
python C:\Users\Donson\.codex\skills\aivideoeditor-visual-moderation\scripts\run_keyword_text_audio_redaction.py `
  --output-dir D:\path\keyword_redaction_demo
```

Use `--transcript <json>` to supply your own timestamped dialogue file.
