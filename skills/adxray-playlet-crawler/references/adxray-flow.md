# AdXRay Flow Notes

Use these notes when `scripts/adxray_playlet_crawler.py` fails at a fragile browser step.

## Login

- Login URL: `https://adxray.dxylds.com/login?redirect=%2F`
- Account input: `input[name='username']` or placeholder `请输入用户名`
- Password input: `input[name='password']` or placeholder `请输入密码`
- Reliable login button: `button.btn-login`
- The button text may render as `登 录`; do not rely only on exact `登录`.
- Treat `抖音热播榜`, `工作台`, `我的素材库`, or `应用中心` as post-login signals.

## Ranking Page

- Direct URL after login: `https://adxray.dxylds.com/rank/distribution`
- Manual menu text: `抖音热播榜`
- Expand filter controls with `.a6de18a4-selector-operation-button:has-text('更多')`.
- Default categories for this workflow: `真人AI`, `沙雕漫`, `2D漫`, `3D漫`, `解说漫`, `游戏编辑器漫`.
- Confirm category selection with `确定` or `确认`.
- Ranking result links are anchors whose `href` contains `/playlet/<id>`.
- Search input observed on the ranking page: placeholder `搜索产品/短剧/小说/公司`.
- If no `--drama-name` is provided, pick the first `/playlet/<id>` link in DOM order.

## Detail Page

- Detail URL pattern: `https://adxray.dxylds.com/playlet/<id>`.
- Relevant tab/text: `素材筛选`.
- Sort labels:
  - `most_exposure`: `最多曝光` / user wording `最高曝光`
  - `most_likes`: `最多点赞` / user wording `最高点赞`
  - `most_plays`: `最多播放` / user wording `最高播放`
- `最多播放` may be hidden until sorting `更多` is expanded.
- Material play buttons currently use `.a6de18a4-cover-video-play-btn`.
- After clicking a play button, a modal contains a real video element:
  - `video.currentSrc` or `video.src`
  - host commonly `https://adxvideo.dataeye.com/...mp4?...`

## Debug Output

The script writes:

- `manifest.json`: selected drama, categories, sort rows, video URLs, output paths, status, bytes.
- `debug/run.log`: timestamped navigation/download notes.
- `debug/*.txt`: page text for the failed or checkpoint page.
- `debug/*.png`: screenshots for failed or checkpoint page.

Patch the narrow selector that failed after inspecting these artifacts. Keep credentials in CLI args or `ADXRAY_ACCOUNT` / `ADXRAY_PASSWORD`; never save them in the skill or manifest.
