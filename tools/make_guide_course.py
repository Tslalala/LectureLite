"""Build the single, screenshot-rich built-in guide. No account/store writes.

Run: python tools/make_guide_course.py [--voice]
Voice synthesis is optional; cached MP3s are reused only for identical narration.

时间轴包含：逐句讲稿（含 批注/重点 元数据）、讲到才出现的高亮/下划线/批注、
光标弧线滑动与点击涟漪、分章节滚动与翻页 —— 与「录制演讲」生成的包同一数据格式。
"""
import argparse
import asyncio
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / 'docs' / 'guide'
VOICE = 'zh-CN-XiaoxiaoNeural'
GAP_MS = 420

# 每章：讲稿句子（口语化短句）+ 标注片段（在该章 markdown 里原样出现）
# dict 句子支持: op=('highlight'|'underline'|'comment', md片段[, 批注内容]), key=True 标重点
SECTIONS = [
    {   # 01 认识主页
        'narration': [
            '欢迎使用 LectureLite。这份指南带你从第一份文档开始，制作、保存并分享自己的课程。',
            '主页就是你的课程库：我的录制、收藏的课程、分享给我的、回收站，四个栏目分开显示。',
            '搜索和排序只作用于当前栏目；有进度的课程，打开时可以继续上次的位置。',
            '点击「新建讲解」进入编辑器。登录之后，保存的课程都归属于你的账号。',
            {'t': '给别人授权时，填写对方的 UID，而不是密码。',
             'op': ('comment', '填写对方的 UID，而不是密码',
                    'UID 是公开身份，密码永远不要发给任何人。')},
        ],
    },
    {   # 02 添加第一份文档
        'narration': [
            '添加文档支持 Markdown、PDF 和 HTML，也可以直接拖进页面。',
            {'t': '文档带图片时，使用「添加文件夹」，把文档和图片一起加入，保留相对路径。',
             'op': ('highlight', '保留相对路径')},
            '多个文档会出现在顶部标签里，点击标签就能切换讲解对象；左侧目录帮你快速定位章节。',
            {'t': '「打包源文件」建议保持勾选，这样听众只需要收到一个讲解包。',
             'op': ('comment', '「打包源文件」建议保持勾选',
                    '取消勾选只适合你能另外提供完全相同源文件的场景。')},
            {'t': '注意，不要把含有账号、密钥或隐私内容的文档打包给别人。',
             'op': ('underline', '账号、密钥或隐私内容')},
        ],
    },
    {   # 03 亲自录制
        'narration': [
            '准备好麦克风后，点击「开始录制」，并允许浏览器使用麦克风。',
            '一边讲，一边滚动文档、移动光标或切换文件，这些动作会和声音写进同一条时间轴。',
            {'t': '选中 Markdown 正文，可以使用高亮、下划线、删除线、批注等标注工具。',
             'op': ('highlight', '高亮、下划线、删除线、批注')},
            {'t': '先说明观点，再标注重点，效果通常比整页涂色更清楚。',
             'op': ('comment', '先说明观点，再标注重点', '本指南里的标注就是这样做的。')},
            '完成后点击「停止」，等待音频整理结束。重要内容请及时保存。',
        ],
    },
    {   # 04 AI 讲稿
        'narration': [
            '讲稿可以让 AI 生成，也可以自己写，每个段落一行。',
            '生成后先读一遍，核对事实、术语和发音，再点击「编辑」调整。',
            {'t': '勾选「语音」，选择声音与语速，再点击「生成语音讲解」合成课程。',
             'op': ('highlight', '生成语音讲解')},
            '取消语音，可以先制作只有字幕和时间轴的无声版本，适合先检查内容。',
        ],
    },
    {   # 05 预览后再发布
        'narration': [
            {'t': '完成录制或生成后，先点击「预览讲解」。', 'key': True},
            '播放器里可以暂停、拖动进度条、调整速度，字幕也可以随时开关。',
            {'t': '重点检查三件事：声音听得清吗？页面位置对得上吗？图片和标注完整吗？',
             'op': ('underline', '声音听得清吗'), 'key': True},
            '无语音的课程会显示「无音频」，这是时间轴模式，而不是播放故障。',
        ],
    },
    {   # 06 保存、收藏与分享
        'narration': [
            '「下载讲解包」把 lecture.zip 保存到自己的电脑，适合备份与离线分发。',
            {'t': '「保存到主页」上传到自己的账号，之后从「我的录制」进入。',
             'op': ('comment', '下载不等于上传', '下载≠上传：上传也不会自动对所有人公开。')},
            '打开课程的分享与管理，可以按 UID 授权，或生成带有效期的分享链接。',
            '链接随时可以吊销；取消收藏只是移出列表，不会删除原课程。',
        ],
    },
    {   # 07 删除与恢复
        'narration': [
            {'t': '误删的自建课程会进入回收站，三十天内都可以恢复。',
             'op': ('highlight', '从回收站恢复')},
            {'t': '永久删除不可撤销，过期内容也会自动清理，所以请保留独立备份。',
             'op': ('comment', '独立备份', '回收站不能代替备份，重要课程请导出 zip。')},
            '内置指南是示例内容，不能被删除，也不会占用你的课程库。',
        ],
    },
    {   # 08 用 HTML 讲演示文稿
        'narration': [
            '也可以直接讲 HTML 演示，选择 .html 文件即可。',
            '示例 EXO 演示保留了原来的主题、目录和翻页按钮。',
            '录制时在演示内部翻页和滚动，播放时会按时间轴还原。',
            {'t': 'HTML 在隔离沙箱运行，不能读取登录信息，也不能访问网络。',
             'op': ('highlight', '隔离沙箱')},
            'Canvas 动画和嵌入视频暂不保证重放；陌生模板请先录十秒检查。',
        ],
    },
    {   # 09 完整练习
        'narration': [
            '最后做一次完整练习：加文档、写讲稿，先生成无声预览，再生成语音或亲自录制。',
            '下载备份，保存到主页，加收藏，再测试一次分享链接。',
            {'t': '遇到问题先看顶部状态提示，核对图片路径、网络与浏览器权限。', 'key': True},
            '祝你讲解顺利！',
        ],
    },
]


def mp3_frames(data):
    """Return raw MPEG layer-III frames and their exact sample-based duration."""
    i, seconds, frames = 0, 0.0, bytearray()
    if data[:3] == b'ID3':
        i = 10 + sum((data[6+j] & 127) << (7*(3-j)) for j in range(4))
    while i + 4 <= len(data):
        h = int.from_bytes(data[i:i+4], 'big')
        version, layer = (h >> 19) & 3, (h >> 17) & 3
        bi, si = (h >> 12) & 15, (h >> 10) & 3
        if h >> 21 != 0x7ff or version == 1 or layer != 1 or bi in (0, 15) or si == 3:
            i += 1
            continue
        rates = [44100, 48000, 32000]
        sample_rate = rates[si] // (1 if version == 3 else 2 if version == 2 else 4)
        table = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320] if version == 3 else [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
        length = (144 if version == 3 else 72) * table[bi] * 1000 // sample_rate + ((h >> 9) & 1)
        if i + length > len(data):
            break
        frames.extend(data[i:i+length])
        seconds += (1152 if version == 3 else 576) / sample_rate
        i += length
    if not frames:
        raise ValueError('No valid MP3 frames')
    return bytes(frames), round(seconds * 1000)


async def voices():
    import edge_tts
    cache = ROOT / 'demo' / '.guide-audio'
    cache.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(2)
    flat = [s['t'] if isinstance(s, dict) else s
            for sec in SECTIONS for s in sec['narration']]
    async def one(text):
        path = cache / (hashlib.sha256((VOICE + text).encode()).hexdigest()[:20] + '.mp3')
        async with sem:
            if not path.exists():
                last = None
                for v in (VOICE, 'zh-CN-YunxiNeural'):
                    for attempt in range(3):
                        try:
                            await asyncio.wait_for(
                                edge_tts.Communicate(text, v).save(str(path)), 60)
                            last = None
                            break
                        except Exception as e:
                            last = e
                            await asyncio.sleep(1.2 * (attempt + 1))
                    if not last:
                        break
                    path = cache / (hashlib.sha256((v + text).encode()).hexdigest()[:20] + '.mp3')
                if last:
                    raise RuntimeError(f'句子合成失败 {text[:24]!r}: {last}')
            try:
                return mp3_frames(path.read_bytes())
            except ValueError:
                path.unlink(missing_ok=True)   # 缓存损坏（如 0 字节）→ 重新合成
                raise
    res = await asyncio.gather(*(one(t) for t in flat))
    it = iter(res)
    return [[(s['t'] if isinstance(s, dict) else s, next(it))
             for s in sec['narration']] for sec in SECTIONS]


def build(with_voice=False):
    md = (GUIDE / '教你使用lecturelite.md').read_text(encoding='utf-8')
    chunks = re.split(r'(?m)^## ', md)[1:]
    assert len(chunks) == len(SECTIONS), (len(chunks), len(SECTIONS))
    refs = set(re.findall(r'!\[[^\]]*\]\(([^)]+)\)', md))
    for ref in refs:
        if not (GUIDE / ref).is_file():
            raise FileNotFoundError(f'Missing screenshot: {ref}')
    audio = asyncio.run(voices()) if with_voice else [
        [(s['t'] if isinstance(s, dict) else s,
          (b'', max(3500, len(s['t'] if isinstance(s, dict) else s) * 280)))
         for s in sec['narration']] for sec in SECTIONS]

    meta = {'v': 3, 'app': 'LectureLite', 'created': 'guide-2026-09',
            'viewport': {'w': 1280, 'h': 720}, 'files': [], 'fileSwitches': [],
            'states': [], 'cursor': [], 'clicks': [], 'annotations': [],
            'annotationPopups': [], 'script': []}
    payload = io.BytesIO()
    t = 0.0
    ann_seq = 0
    pos = {'x': 0.32, 'y': 0.22}
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as z:
        for i, (chunk, spoken) in enumerate(zip(chunks, audio)):
            name = f'{i+1:02d}-{chunk.splitlines()[0].split("·")[-1].strip()}.md'
            file_text = '# ' + chunk
            meta['files'].append({'name': name, 'mime': 'text/markdown', 'type': 'md'})
            meta['fileSwitches'].append({'t': round(t), 'fi': i})
            z.writestr(name, file_text)

            n = len(spoken)
            for si, (sentence, (_, duration)) in enumerate(spoken):
                start = t
                end = t + duration
                # 章内滚动：随句推进 0 → 0.94（截图页滚动幅度真实可感）
                sr = round(min(0.94, (si + 0.6) / max(1, n) * 0.94), 4)
                meta['states'].append({'t': round(start), 'sr': sr, 'y': 0, 's': None})
                meta['script'].append({'t': round(start), 'text': sentence})

                # 标注：句里提到哪个片段，就「讲到才画上去」（+400ms）
                entry = next(s for s in SECTIONS[i]['narration']
                             if (s['t'] if isinstance(s, dict) else s) == sentence)
                op = entry.get('op') if isinstance(entry, dict) else None
                frag = op[1] if op else None
                off = file_text.find(frag) if frag else -1
                if op and off >= 0:
                    ann_seq += 1
                    ann = {'id': f'ann-guide-{ann_seq}', 't': round(start + 400),
                           'type': op[0], 'fi': i, 'quote': frag,
                           'offsets': [off, off + len(frag)]}
                    if op[0] == 'comment':
                        ann['text'] = op[2]
                        meta['annotationPopups'].append(
                            {'t': round(start + 1100), 'id': ann['id'], 'fi': i, 'open': True})
                        meta['annotationPopups'].append(
                            {'t': round(end + 400), 'id': ann['id'], 'fi': i, 'open': False})
                    meta['annotations'].append(ann)
                    meta['states'].append({'t': round(start + 400), 'sr': sr, 'y': 0,
                                           's': {'a': off, 'b': off + len(frag)}})
                elif op:
                    print(f'警告：片段未找到，跳过标注: {frag!r} (第{i+1}章)')

                # 光标：句间弧线滑移 + 句内心跳；句首一次点击涟漪
                target = {'x': round(0.30 + 0.09 * ((si % 3) - 1), 4),
                          'y': round(min(0.86, 0.18 + 0.62 * ((si + 0.5) / max(1, n))), 4)}
                mid = {'x': round((pos['x'] + target['x']) / 2 + (0.03 if si % 2 else -0.03), 4),
                       'y': round((pos['y'] + target['y']) / 2 - 0.02, 4)}
                for tt, p in ((start - GAP_MS * 0.6, pos),
                              (start - GAP_MS * 0.25, mid), (start, target)):
                    meta['cursor'].append({'t': round(tt), 'x': p['x'], 'y': p['y']})
                meta['cursor'].append({'t': round(start + duration * 0.6),
                                       'x': target['x'], 'y': target['y']})
                meta['clicks'].append({'t': round(start), 'x': target['x'], 'y': target['y']})
                pos = target

                t = end + GAP_MS
        meta['duration'] = t / 1000
        meta['file'] = meta['files'][0]
        for key in ('states', 'cursor', 'clicks', 'annotationPopups'):
            meta[key].sort(key=lambda x: x['t'])
        meta['script'].sort(key=lambda x: x['t'])
        meta['annotations'].sort(key=lambda x: x['t'])
        z.writestr('lecture.json', json.dumps(meta, ensure_ascii=False))
        if with_voice:
            z.writestr('audio.mp3', b''.join(a for sec in audio for _, (a, _) in sec))
        for ref in refs:
            z.write(GUIDE / ref, ref)
    dest = ROOT / 'demo' / '教你使用lecturelite.lecture.zip'
    dest.parent.mkdir(exist_ok=True)
    dest.write_bytes(payload.getvalue())
    print(f'{dest.name}: {len(chunks)} chapters, {len(meta["annotations"])} annotations, '
          f'{len(meta["cursor"])} cursor samples, {len(meta["script"])} sentences, '
          f'{t/1000:.1f}s, voice={with_voice}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--voice', action='store_true')
    build(parser.parse_args().voice)
