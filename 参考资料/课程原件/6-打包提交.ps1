# =====================================================================
#  大作业材料打包（中期与最终通用）
#  课程组提供
#
#  怎么用：
#    1. 把 打包提交.ps1 和 打包提交.cmd 一起复制到你的项目文件夹最外层。
#       课程组发下来的这两个文件名前面带编号（例如 6-打包提交.ps1），
#       不用改名，脚本会自己认。
#    2. 双击 打包提交.cmd
#    3. 按提示输入学号、姓名、哪一门课、这次交中期还是最终
#
#  这个脚本只读你的项目，不会改动你的代码。
#  它会在项目文件夹旁边新建一个提交文件夹，并压成一个 zip。
# =====================================================================

function Say {
    param([string]$Text, [string]$Color = 'Gray')
    Write-Host $Text -ForegroundColor $Color
}

# 压缩时的进度条在部分受限控制台下会报错，这里直接关掉
$ProgressPreference = 'SilentlyContinue'

Say ''
Say '===============  大作业材料打包  ===============' 'Cyan'
Say ''
Say '接下来有四个问题，输入后按回车。'
Say ''

$sid  = (Read-Host '学号（例如 20231234）').Trim()
$name = (Read-Host '姓名（例如 张三）').Trim()
Say ''
Say '课程：   1 = 软件编程        2 = 高级程序设计语言与训练'
$pick = (Read-Host '请输入 1 或 2（哪一门课）').Trim()

if ($pick -eq '2') {
    $course = '高级程序设计语言与训练'
    $due    = '11 月 20 日'
} else {
    $course = '软件编程'
    $due    = '11 月 17 日'
}
Say ''
Say '这次交的是：   1 = 中期材料        2 = 最终材料'
$stagePick = (Read-Host '请输入 1 或 2（中期还是最终）').Trim()
if ($stagePick -eq '2') { $stageName = '最终' } else { $stageName = '中期' }
$dueMid = '10 月 22 日'
Say ''

if (($sid -eq '') -or ($name -eq '')) {
    Say '学号或姓名没有填，脚本退出。请重新双击运行。' 'Red'
    Read-Host '按回车关闭窗口'
    exit 1
}

if ($PSScriptRoot) { $project = $PSScriptRoot } else { $project = (Get-Location).Path }
$parent = Split-Path $project -Parent
if ([string]::IsNullOrWhiteSpace($parent)) { $parent = $project }

$folder = "$course-$sid-$name-${stageName}材料"
$stage  = Join-Path $parent $folder
$zip    = Join-Path $parent "$folder.zip"

Say "项目文件夹： $project"
Say "输出位置：   $parent"
Say "             （压缩包会生成在这里，不在你的项目文件夹里面）"
Say ''

# 先确认脚本确实放在了项目文件夹里
$probe = @('CLAUDE.md', '要做什么.md', '怎么做.md', '怎么算做对.md',
           '决定记录.md', '选题自查表.md', '大作业报告.docx',
           '中期汇报.pptx', '每日工作记录')
$hit = 0
foreach ($f in $probe) { if (Test-Path (Join-Path $project $f)) { $hit = $hit + 1 } }
if ($hit -eq 0) {
    Say '这个文件夹里没有找到你的项目文件。' 'Yellow'
    Say '脚本找的是这些固定文件名：CLAUDE.md、要做什么.md、怎么做.md、怎么算做对.md、' 'Yellow'
    Say '决定记录.md、选题自查表.md、大作业报告.docx、中期汇报.pptx、每日工作记录。' 'Yellow'
    Say '如果它们确实不在这里，说明脚本放错了文件夹，请把它复制到项目文件夹最外层。' 'Yellow'
    Say ''
    $go = (Read-Host '仍然继续吗？继续请输 y，否则直接回车退出').Trim()
    if ($go -ne 'y') { exit 1 }
}

if (Test-Path $stage) {
    Say "上次留下的暂存文件夹还在，会先清掉再重建：" 'Yellow'
    Say "  $stage" 'Yellow'
    Remove-Item $stage -Recurse -Force
}
New-Item -ItemType Directory -Path $stage | Out-Null

$found   = New-Object System.Collections.ArrayList
$missing = New-Object System.Collections.ArrayList

# ---------- 01 大作业报告 ----------
$srcReport = Join-Path $project '大作业报告.docx'
if (Test-Path $srcReport) {
    Copy-Item $srcReport (Join-Path $stage '01-大作业报告.docx') -Force
    [void]$found.Add('大作业报告.docx')
} else {
    [void]$missing.Add('大作业报告.docx')
}

# ---------- 02 中期汇报 ----------
$srcPptx = Join-Path $project '中期汇报.pptx'
$srcPdf  = Join-Path $project '中期汇报.pdf'
if (Test-Path $srcPptx) {
    Copy-Item $srcPptx (Join-Path $stage '02-中期汇报.pptx') -Force
    [void]$found.Add('中期汇报.pptx')
} else {
    [void]$missing.Add('中期汇报.pptx')
}
if (Test-Path $srcPdf) { Copy-Item $srcPdf (Join-Path $stage '02-中期汇报.pdf') -Force }

# ---------- 03 项目存档 ----------
$git = Get-Command git -ErrorAction SilentlyContinue
$bundleMade = $false
if ($git -and (Test-Path (Join-Path $project '.git'))) {
    Push-Location $project
    & git bundle create (Join-Path $stage '03-项目存档.bundle') --all 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $bundleMade = $true }
    Pop-Location
}
if ($bundleMade) {
    [void]$found.Add('项目存档.bundle')
} else {
    [void]$missing.Add('项目存档.bundle（没有找到版本存档记录，见说明书第二节动作五）')
}

# ---------- 04 每日工作记录 ----------
$srcDaily = Join-Path $project '每日工作记录'
if (Test-Path $srcDaily) {
    Copy-Item $srcDaily (Join-Path $stage '04-每日工作记录') -Recurse -Force
    $days = (Get-ChildItem $srcDaily -Directory | Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' }).Count
    [void]$found.Add("每日工作记录（$days 天）")
} else {
    [void]$missing.Add('每日工作记录')
}

# ---------- 05 三份说明书 ----------
$specDir = Join-Path $stage '05-三份说明书'
New-Item -ItemType Directory -Path $specDir | Out-Null
$specs = @('要做什么.md', '怎么做.md', '怎么算做对.md')
foreach ($s in $specs) {
    $p = Join-Path $project $s
    if (Test-Path $p) {
        Copy-Item $p (Join-Path $specDir $s) -Force
        [void]$found.Add("三份说明书・$s")
    } else {
        [void]$missing.Add("三份说明书・$s")
    }
}
if ((Get-ChildItem $specDir).Count -eq 0) { Remove-Item $specDir -Force }

# ---------- 06 到 08 ----------
$simple = @(
    @('06-选题自查表.md', '选题自查表.md', '选题自查表'),
    @('07-决定记录.md',   '决定记录.md',   '决定记录'),
    @('08-项目规矩-CLAUDE.md', 'CLAUDE.md', '项目规矩 CLAUDE.md')
)
foreach ($item in $simple) {
    $p = Join-Path $project $item[1]
    if (Test-Path $p) {
        Copy-Item $p (Join-Path $stage $item[0]) -Force
        [void]$found.Add($item[2])
    } else {
        [void]$missing.Add($item[2])
    }
}

# ---------- 09 材料清单 ----------
$now = Get-Date -Format 'yyyy-MM-dd HH:mm'
$lines = New-Object System.Collections.ArrayList
[void]$lines.Add('# 材料清单')
[void]$lines.Add('')
[void]$lines.Add("项目：$folder")
[void]$lines.Add("阶段：${stageName}材料")
[void]$lines.Add("学号：$sid　姓名：$name　课程：$course")
[void]$lines.Add("打包时间：$now")
[void]$lines.Add('')
[void]$lines.Add('| 序号 | 文件 | 是什么 |')
[void]$lines.Add('| --- | --- | --- |')
[void]$lines.Add('| 01 | 大作业报告.docx | 文字版的完整交代：问题、需求、设计、实现、测试、过程 |')
[void]$lines.Add('| 02 | 中期汇报.pptx | 上台要讲的十页 |')
[void]$lines.Add('| 03 | 项目存档.bundle | 从第一天到今天的全部修改记录，可完整还原开发过程 |')
[void]$lines.Add('| 04 | 每日工作记录/ | 逐日的工作记录与截图 |')
[void]$lines.Add('| 05 | 三份说明书/ | 要做什么、怎么做、怎么算做对 |')
[void]$lines.Add('| 06 | 选题自查表.md | 七项规模指标的填报 |')
[void]$lines.Add('| 07 | 决定记录.md | 自己做过的决定 |')
[void]$lines.Add('| 08 | 项目规矩-CLAUDE.md | 给 AI 定的项目规矩 |')
[void]$lines.Add('| 09 | 材料清单.md | 本文件 |')
[void]$lines.Add('')
[void]$lines.Add('## 缺哪样')
[void]$lines.Add('')
if ($missing.Count -eq 0) {
    [void]$lines.Add('九样齐全。')
} else {
    foreach ($m in $missing) { [void]$lines.Add("- 缺：$m") }
}
[void]$lines.Add('')
$listText = ($lines -join "`r`n") + "`r`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $stage '09-材料清单.md'), $listText, $utf8NoBom)
[void]$found.Add('材料清单.md')

# ---------- 交之前扫一遍密钥 ----------
$keyHits = @()
$scanExt = @('.md', '.txt', '.json', '.py', '.js', '.ts', '.env', '.yml', '.yaml', '.ini', '.cfg')
Get-ChildItem $project -Recurse -File -ErrorAction SilentlyContinue | Where-Object {
    $scanExt -contains $_.Extension.ToLower()
} | ForEach-Object {
    $one = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
    if ($one -and ($one -match 'sk-[A-Za-z0-9_\-]{16,}' -or
                   $one -match '(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret).{0,3}[:=]')) {
        $keyHits += $_.FullName.Substring($project.Length).TrimStart('\')
    }
}
if ($keyHits.Count -gt 0) {
    Say ''
    Say '注意：下面这些文件里像是写进了密钥或令牌。' 'Red'
    foreach ($h in $keyHits) { Say "  $h" 'Red' }
    Say '密钥不应该出现在任何要交的文件里。请把它挪到项目文件夹之外的本地配置里，' 'Red'
    Say '改完再重新打包。' 'Red'
    Say ''
    Say '另外提醒：项目存档里会带上你历史提交过的全部内容。如果曾经把密钥提交过，' 'Red'
    Say '即使后来删掉了，也可能留在存档里。这一项脚本查不出来，请自己确认。' 'Red'
    Say ''
    $goKey = (Read-Host '若确认这些只是示例文字、不是真密钥，请输入「确认不是密钥」继续；直接回车就停止').Trim()
    if ($goKey -ne '确认不是密钥') {
        Say '已停止，没有生成压缩包。请先把密钥挪出项目文件夹再重跑。' 'Yellow'
        exit 1
    }
}

# ---------- 压缩 ----------
Say ''
Say '正在压缩……' 'Cyan'
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $stage -DestinationPath $zip -Force

$sizeMB = [math]::Round((Get-Item $zip).Length / 1MB, 2)

Say ''
Say '=================  打包完成  =================' 'Green'
Say ''
Say "压缩包： $zip" 'Green'
Say "大小：   $sizeMB MB"
Say ''
Say "收进压缩包的内容：" 'White'
foreach ($f in $found) { Say "   ok  $f" }
if ($missing.Count -gt 0) {
    Say ''
    Say "缺少 $($missing.Count) 样：" 'Yellow'
    foreach ($m in $missing) { Say "   缺  $m" 'Yellow' }
    Say ''
    Say '缺的就空着交，不要临时补造，材料清单里会写清楚缺了什么。' 'Yellow'
    Say '缺的那几样按该项未完成计分。' 'Yellow'
}
Say ''
Say '提醒：压缩包里只有这九样材料，没有你的源代码。' 'Cyan'
Say '      源代码留在项目文件夹里别删，验收时要当场把它跑起来。' 'Cyan'
Say ''
Say '下一步：' 'Cyan'
Say "  1. 把这个压缩包当附件发邮件给课程组"
Say "  2. 邮件标题写：【$course】${stageName}材料 $sid $name"
if ($stageName -eq '中期') {
    Say "  3. 中期材料截止：${dueMid}上课前"
    Say "     本科目最终交付：$due（上机课）"
} else {
    Say "  3. 最终材料截止：$due（上机课）"
}
if ($sizeMB -gt 20) {
    Say ''
    Say "注意：压缩包超过 20MB。" 'Yellow'
    Say '先把 每日工作记录 里的截图转成 JPG，再重跑一次本脚本；仍超就分两封邮件发。' 'Yellow'
    Say '转换办法：用 Windows 截图工具时，另存为直接选 JPEG；已经存成 PNG 的，' 'Yellow'
    Say '用画图打开，选「文件 → 另存为 → JPEG 图片」，体积通常能小一个数量级。' 'Yellow'
}
Say ''

Read-Host '按回车关闭窗口'
