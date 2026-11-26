# Roku setup

Projecter can play on a Roku in two ways:

| | Projecter channel (recommended) | Media Assistant (fallback) |
|---|---|---|
| Setup | One-time Developer Mode + install script | Add a free channel from the Roku store |
| Subtitles | Drawn by the Roku: track, size, background, position and timing change **instantly** while the video keeps playing | Drawn into the picture: every change restarts the video |
| Pause / skip / TV remote | ✓ | ✓ |

Projecter picks the Projecter channel automatically when it's installed, and falls back to
Media Assistant otherwise.

---

## 1. Basics (both ways)

1. Put the Roku and the laptop on the **same Wi-Fi network**.
2. Allow network control: on the Roku go to **Settings → System → Advanced system settings →
   Control by mobile apps → Network access** and set it to **Enabled**
   (Roku's *Limited* mode blocks most commands).
3. Start Projecter. The Roku shows up in the TV list within a few seconds.

## 2a. Projecter channel (instant subtitle changes)

### Turn on Developer Mode (once)

1. Go to the Roku **home screen**.
2. With the Roku remote, press this sequence **quickly** (about 3–4 seconds in total, no pauses):

   **Home, Home, Home, Up, Up, Right, Left, Right, Left, Right**

3. The **Developer Settings** screen opens. Note the IP address it shows
   (e.g. `http://192.168.1.50`).
4. Choose **Enable installer and restart**, accept the agreement, and pick a password.
5. The Roku restarts.

> **The screen doesn't appear?** The timing is picky. Start from the home screen, press the
> whole sequence faster, and point the remote at the TV. It often takes 2–3 tries.
> Sending the sequence over the network (from an app or script) does **not** work: it has to
> be the physical remote.

### Install the channel

From the Projecter folder on the laptop:

```
python install_roku_channel.py <roku-ip> <password>
```

For example `python install_roku_channel.py 192.168.1.50 mypassword`. You should see
`Projecter channel installed on the Roku.` and a **Projecter** channel appears on the Roku.

The IP and password are saved in `config.json` (which is never committed), so after updating
Projecter you can reinstall the channel with just:

```
python install_roku_channel.py
```

### Use it

Cast anything as usual (web app or the browser extension). Open **Subtitles**, pick a track or
add your own `.srt`, and drag the sliders. The TV follows along while you drag.

## 2b. Media Assistant (no Developer Mode)

On the Roku: **Home → Streaming Store → search "Media Assistant" → Add channel**. That's it.
Subtitles still work, but each change restarts the video for a few seconds.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Roku isn't listed | Same Wi-Fi? Network access set to *Enabled* (step 1.2)? Click **Scan** in the web app. |
| "Your Roku is blocking network control" | Network access is *Limited* or *Disabled*; see step 1.2. |
| Install script: "Couldn't reach the Roku installer" | Developer Mode is off (redo 2a) or the password is wrong. Check the Roku's IP in **Settings → Network → About**. |
| Black screen with the title on top | Restart Projecter so it runs the latest version; it converts videos the Roku can't decode. If it persists, check the console output. |
| Subtitles change but the video restarts | The Projecter channel isn't installed (or was removed), so Media Assistant is being used; see 2a. |
| Developer Mode on a Roku you want back to normal | Repeat the button sequence and choose **Disable installer and restart**. This removes the Projecter channel. |

## For developers

The channel source is in [`roku/`](roku/) (BrightScript / SceneGraph). Projecter launches it
with the stream URL and sends subtitle changes to the running channel through Roku's ECP
`/input` command; the channel fetches the subtitle lines from
`http://<laptop>:8765/roku/<device>/cues`. While Developer Mode is on, the Roku's debug console
(`telnet <roku-ip> 8085`) shows the channel's errors.
