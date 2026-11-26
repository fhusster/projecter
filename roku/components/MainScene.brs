sub init()
    m.video = m.top.findNode("video")
    m.subs = m.top.findNode("subs")
    m.hint = m.top.findNode("hint")
    m.hint.font = "font:MediumSystemFont"
    m.hint.font.size = 42
    m.video.notificationInterval = 0.1
    m.video.observeField("position", "render")
    m.cues = []
    m.offset = 0.0
    m.ctl = ""
    m.shown = invalid
    m.task = invalid
    m.style = {size: 20.0, bg: 0.0, pos: 6.0, delay: 0.0}
end sub

function num(v as dynamic, fallback as float) as float
    if v = invalid then return fallback
    if v = "" then return fallback
    return Val(v)
end function

sub onLaunch()
    a = m.top.launch
    if a <> invalid and a.u <> invalid then play(a)
end sub

sub onInput()
    a = m.top.input
    if a = invalid then return
    if a.u <> invalid
        play(a)
    else if a.op = "style"
        applyStyle(a)
    else if a.op = "cues"
        loadCues()
    end if
end sub

sub play(a as object)
    m.offset = num(a.o, 0.0)
    if a.c <> invalid then m.ctl = a.c
    c = CreateObject("roSGNode", "ContentNode")
    c.url = a.u
    c.streamFormat = "hls"
    if a.t <> invalid then c.title = a.t
    m.video.control = "stop"
    m.video.content = c
    m.video.control = "play"
    m.video.setFocus(true)
    m.hint.visible = false
    m.cues = []
    applyStyle(a)
    loadCues()
end sub

sub applyStyle(a as object)
    s = m.style
    s.size = num(a.size, s.size)
    s.bg = num(a.bg, s.bg)
    s.pos = num(a.pos, s.pos)
    s.delay = num(a.delay, s.delay)
    m.shown = invalid    ' redraw even if the same line is on screen
    render()
end sub

sub loadCues()
    if m.ctl = "" then return
    if m.task <> invalid then m.task.control = "stop"
    m.task = CreateObject("roSGNode", "CuesTask")
    m.task.url = m.ctl + "/cues"
    m.task.observeField("cues", "onCues")
    m.task.control = "run"
end sub

sub onCues()
    m.cues = m.task.cues
    if m.cues = invalid then m.cues = []
    m.shown = invalid
    render()
end sub

' ---- drawing: same maths as Projecter's burned-in subtitles (288-line canvas -> 1080 px)
sub render()
    t = m.offset + m.video.position - m.style.delay
    text = ""
    n = m.cues.Count()
    if n > 0
        lo = 0
        hi = n - 1
        k = -1
        while lo <= hi
            mid = Int((lo + hi) / 2)
            if m.cues[mid][0] <= t
                k = mid
                lo = mid + 1
            else
                hi = mid - 1
            end if
        end while
        i = k
        while i >= 0 and i > k - 4    ' overlapping cues: show them together
            c = m.cues[i]
            if c[1] > t
                if text = "" then text = c[2] else text = c[2] + Chr(10) + text
            end if
            i = i - 1
        end while
    end if
    if m.shown <> invalid
        if m.shown = text then return
    end if
    m.shown = text
    draw(text)
end sub

function makeLabel(text as string, px as integer, color as string) as object
    l = CreateObject("roSGNode", "Label")
    l.font = "font:MediumBoldSystemFont"
    l.font.size = px
    l.color = color
    l.text = text
    return l
end function

' too wide for the screen: break it at the space nearest the middle
function splitLine(line as string, px as integer) as object
    if makeLabel(line, px, "0xFFFFFFFF").boundingRect().width <= 1800 then return [line]
    half = Int(Len(line) / 2)
    for d = 0 to half
        if Mid(line, half + d + 1, 1) = " " then return [Left(line, half + d).Trim(), Mid(line, half + d + 2).Trim()]
        if half - d > 0 and Mid(line, half - d + 1, 1) = " " then return [Left(line, half - d).Trim(), Mid(line, half - d + 2).Trim()]
    end for
    return [line]
end function

sub draw(text as string)
    m.subs.removeChildrenIndex(m.subs.getChildCount(), 0)
    if text = "" then return
    px = Int(m.style.size * 3.75 + 0.5)
    lines = []
    for each raw in text.Split(Chr(10))
        line = raw.Trim()
        if line <> ""
            for each part in splitLine(line, px)
                lines.Push(part)
            end for
        end if
    end for
    lineH = Int(px * 1.2)
    bottom = 1080 - Int((8 + m.style.pos / 100 * 230) * 3.75)
    y = bottom - lineH * lines.Count()
    boxed = m.style.bg > 0
    pad = 10
    for each line in lines
        main = makeLabel(line, px, "0xFFFFFFFF")
        r = main.boundingRect()
        x = Int((1920 - r.width) / 2)
        ty = y + Int((lineH - r.height) / 2)
        if boxed
            box = CreateObject("roSGNode", "Rectangle")
            box.color = "0x000000FF"
            box.opacity = m.style.bg / 100
            box.width = r.width + pad * 2
            box.height = lineH
            box.translation = [x - pad, y]
            m.subs.appendChild(box)
        else
            ' black outline + soft shadow, like the burned-in style
            for each o in [[-4, -4], [4, -4], [-4, 4], [4, 4], [0, -5], [0, 5], [-5, 0], [5, 0], [3, 6]]
                sh = makeLabel(line, px, "0x000000FF")
                sh.translation = [x + o[0], ty + o[1]]
                m.subs.appendChild(sh)
            end for
        end if
        main.translation = [x, ty]
        m.subs.appendChild(main)
        y = y + lineH
    end for
end sub
