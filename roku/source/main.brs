' Projecter's own Roku channel: plays the video Projecter sends and draws the subtitles
' itself, so subtitle changes from the laptop show up instantly (no restart).
sub Main(args as dynamic)
    screen = CreateObject("roSGScreen")
    port = CreateObject("roMessagePort")
    screen.SetMessagePort(port)
    inp = CreateObject("roInput")
    inp.SetMessagePort(port)
    scene = screen.CreateScene("MainScene")
    screen.Show()
    scene.launch = args
    while true
        msg = wait(0, port)
        t = type(msg)
        if t = "roSGScreenEvent"
            if msg.IsScreenClosed() then return
        else if t = "roInputEvent"
            ' Projecter sends op=style (sliders), op=cues (new track / off), or a new video (u=...)
            scene.input = msg.GetInfo()
        end if
    end while
end sub
