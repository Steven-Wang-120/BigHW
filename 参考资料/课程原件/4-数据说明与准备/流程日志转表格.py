# -*- coding: utf-8 -*-
"""
把流程日志转成能用表格软件打开的 CSV。

你会用到的场景：
    BPI Challenge 2020 的报销审批数据下载下来是 .xes.gz 文件，
    这是一种叫 XES 的格式（流程挖掘领域专用），用 Excel 和 WPS 都打不开。
    这个脚本把它转成一张普通的表格。

怎么用：
    1. 确认电脑上装好了 Python（第 1 讲讲过的那个），终端敲 python --version 能出版本号
    2. 把下载到的 .xes.gz 文件和本脚本放在同一个文件夹里
    3. 双击同目录的「运行转换.cmd」，或者在这个文件夹打开终端执行：
           python 流程日志转表格.py RequestForPayment.xes.gz
       不写文件名也行，脚本会自动找这个文件夹里的 .xes 和 .xes.gz
    4. 同目录会生成 events.csv，用 Excel 或 WPS 直接打开

转出来是什么样：
    一行 = 一条事件。列包括
        案例号     这一串事件属于哪一张申请单
        活动名     这一步发生了什么（提交、审批、付款……）
        时间戳     这一步发生的时间
        执行人     谁办的
        以及原始日志里带的所有案例属性（比如申请金额、部门）

    一个案例有多行，按时间排序就是它的完整经过。

遇到问题：
    - 提示找不到 Python  → 回到第 1 讲的《环境部署指南》重装，注意勾选 Add python.exe to PATH
    - 转出来是乱码       → 本脚本写成带 BOM 的 UTF-8，Excel 双击打开应当正常；
                            若仍乱码，用 WPS 打开，或在 Excel 里用「数据 → 从文本/CSV」导入并选 UTF-8
    - 文件很大转得慢     → 正常。三万条事件大约几秒，三十万条约一分钟，等它跑完
"""

import csv
import glob
import gzip
import os
import sys
import xml.etree.ElementTree as ET


def pause():
    "双击运行时停一下让用户看结果；非交互环境下不报错。"
    try:
        input()
    except EOFError:
        pass


def ask(prompt, default=''):
    "问一句并读入回答；非交互环境下返回默认值，不报错。"
    try:
        return input(prompt).strip()
    except EOFError:
        return default


# XES 里这几种属性元素都是"一个键一个值"，统一按字符串读
SCALAR_TAGS = ('string', 'date', 'int', 'float', 'boolean', 'id')

# 下面三样换个更好懂的中文列名，其余属性保持原样
RENAME = {
    'concept:name': None,       # 事件里叫「活动名」，案例里叫「案例号」
    'time:timestamp': '时间戳',
    'org:resource': '执行人',
}


def strip_ns(tag):
    return tag.split('}')[-1] if '}' in tag else tag


def open_log(path):
    if path.lower().endswith('.gz'):
        return gzip.open(path, 'rb')
    return open(path, 'rb')


def read_attr(el):
    return el.get('key'), el.get('value')


def convert(path, out_path):
    cases = 0
    events = 0
    case_keys = []
    event_keys = []
    rows = []

    fh = open_log(path)
    try:
        context = ET.iterparse(fh, events=('start', 'end'))
        _, root = next(context)          # 根节点 log
        trace = None
        event = None
        for evt, el in context:
            name = strip_ns(el.tag)
            if evt == 'start':
                if name == 'trace':
                    trace = {'case': {}, 'events': []}
                elif name == 'event':
                    event = {}
                continue
            # ---- end ----
            if name in SCALAR_TAGS:
                key, val = read_attr(el)
                if key is None:
                    continue
                if event is not None and trace is not None:
                    event[key] = val
                    if key not in event_keys:
                        event_keys.append(key)
                elif trace is not None:
                    trace['case'][key] = val
                    if key not in case_keys:
                        case_keys.append(key)
                el.clear()
            elif name == 'event':
                if trace is not None and event is not None:
                    trace['events'].append(event)
                event = None
                el.clear()
            elif name == 'trace':
                if trace is not None:
                    cases += 1
                    cid = trace['case'].get('concept:name', 'case-%d' % cases)
                    for item in trace['events']:
                        row = {'案例号': cid,
                               '活动名': item.get('concept:name', ''),
                               '时间戳': item.get('time:timestamp', ''),
                               '执行人': item.get('org:resource', '')}
                        for k, v in trace['case'].items():
                            if k == 'concept:name':
                                continue
                            row[k] = v
                        for k, v in item.items():
                            if k in ('concept:name', 'time:timestamp', 'org:resource'):
                                continue
                            row[k] = v
                        rows.append(row)
                        events += 1
                trace = None
                root.clear()
    finally:
        fh.close()

    fixed = ['案例号', '活动名', '时间戳', '执行人']
    extra = []
    for row in rows:
        for k in row:
            if k not in fixed and k not in extra:
                extra.append(k)
    columns = fixed + extra

    with open(out_path, 'w', encoding='utf-8-sig', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return cases, events, columns


def main():
    folder = os.path.dirname(os.path.abspath(__file__))
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not os.path.isabs(target):
            target = os.path.join(folder, target)
    else:
        found = []
        for pat in ('*.xes.gz', '*.xes'):
            found += glob.glob(os.path.join(folder, pat))
        if not found:
            print('没有在这个文件夹里找到 .xes 或 .xes.gz 文件。')
            print('请把下载到的日志文件和本脚本放在一起，再把文件名写在命令后面。')
            pause()
            return 1
        if len(found) > 1:
            print('这个文件夹里有 %d 个日志文件，请选一个：' % len(found))
            for i, one in enumerate(found, 1):
                print('  %d) %s（%.0f KB）' % (
                    i, os.path.basename(one), os.path.getsize(one) / 1024.0))
            pick = ask('输入序号，直接回车就选第 1 个：')
            if pick.isdigit() and 1 <= int(pick) <= len(found):
                target = found[int(pick) - 1]
            else:
                target = found[0]
        else:
            target = found[0]
        print('选中的文件：%s' % os.path.basename(target))

    if not os.path.exists(target):
        print('找不到文件：%s' % target)
        pause()
        return 1

    out_path = os.path.join(folder, 'events.csv')
    print('正在读取 %s ……' % os.path.basename(target))
    try:
        cases, events, columns = convert(target, out_path)
    except ET.ParseError as exc:
        print('')
        print('这个文件不是有效的 XES 日志，读不出来：%s' % exc)
        print('请确认下载的是 .xes 或 .xes.gz 文件，没有解压坏、也没有改过格式。')
        pause()
        return 1
    except UnicodeDecodeError as exc:
        print('')
        print('这个文件的文字编码读不出来：%s' % exc)
        print('XES 日志一般用 UTF-8。若你的文件是别的编码，请把文件名与报错发给课程组。')
        pause()
        return 1
    except (OSError, EOFError) as exc:
        print('')
        print('文件读不动，可能没下载完整：%s' % exc)
        print('请重新下载一次再试。')
        pause()
        return 1

    print('')
    print('转换完成。')
    print('  案例数：%d' % cases)
    print('  事件数：%d' % events)
    print('  列    ：%s' % '、'.join(columns[:8]) + ('……' if len(columns) > 8 else ''))
    print('  输出  ：%s' % out_path)
    print('')
    print('下一步：用 Excel 或 WPS 打开 events.csv。')
    print('一行是一条事件，同一个案例号的行按时间戳排序，就是这张申请单的完整经过。')
    pause()
    return 0


if __name__ == '__main__':
    sys.exit(main())
