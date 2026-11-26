sub init()
    m.top.functionName = "fetch"
end sub

' Downloads the subtitle lines from Projecter: {"cues": [[start, end, "text"], ...]}
sub fetch()
    x = CreateObject("roUrlTransfer")
    x.SetUrl(m.top.url)
    x.SetMinimumTransferRate(1, 10)
    data = ParseJson(x.GetToString())
    if data <> invalid and data.cues <> invalid
        m.top.cues = data.cues
    else
        m.top.cues = []
    end if
end sub
