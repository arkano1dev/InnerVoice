#!/usr/bin/env python3
"""Ubuntu graphical client. Capture, network, and UI use independent threads."""
import io
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
import wave

import requests

from provider import Client, atomic

HOME=Path.home()/".local/state/innervoice-desktop"
CONFIG=Path.home()/".config/innervoice-desktop/settings.json"
UNIT="innervoice-desktop.service"
BLOCK=960000


def check_gateway(cfg):
    caps=Client(cfg).call("GET","/v1/capabilities",timeout=8)
    if caps.get("api_version")!=1 or not caps.get("sessions") or caps.get("fragment_seconds")!=30:
        raise ValueError("Gateway needs the session API and 30-second fragment support")


def setup():
    cfg=json.loads(CONFIG.read_bytes()) if CONFIG.exists() else {}
    for key,default in dict(url="",ca="",token_file="",source_ip="",source_port=0,
                            hotkey="<ctrl>+<alt>+v",microphone="").items():
        value=input(f"{key} [{cfg.get(key,default)}]: ").strip()
        cfg[key]=value or cfg.get(key,default)
    check_gateway(cfg)
    if cfg["ca"] and not Path(cfg["ca"]).is_file():
        raise ValueError("Missing CA file")
    CONFIG.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic(CONFIG,json.dumps(cfg).encode())


def capture(directory,stop,events,device):
    import sounddevice as sd
    try:
        with sd.RawInputStream(samplerate=16000,channels=1,dtype="int16",device=device or None) as mic:
            for index in range(120):
                path=directory/f"{index:03d}.raw"
                with path.open("xb") as out:
                    while out.tell()<BLOCK and not stop.is_set():
                        audio,overflow=mic.read(min(4000,(BLOCK-out.tell())//2))
                        if overflow:
                            raise ValueError("Capture overflow")
                        out.write(bytes(audio))
                        out.flush()
                        os.fsync(out.fileno())
                if stop.is_set():
                    break
    except Exception:
        events.put(("notice","Capture interrupted. Saved audio can be recovered."))
    finally:
        stop.set()
        events.put(("stopped",None))


def network(cfg, directory, key, events, finished):
    try:
        client = Client(cfg)
    except (OSError, ValueError, KeyError):
        events.put(("retry", ("Check the desktop connection settings and credential file.", False)))
        return

    path = None
    while True:
        try:
            files = sorted(directory.glob("*.raw"))
            done = finished.is_set()
            ready = [p for p in files if p.stat().st_size == BLOCK or (done and p.stat().st_size >= 2)]
            if path is None and not ready:
                if done:
                    events.put(("ready", "Empty recording"))
                    return
                finished.wait(1)
                continue
            if path is None:
                session = client.call("POST", "/v1/sessions", headers={"Idempotency-Key": key})
                path = "/v1/sessions/" + session["session_id"]
            progress = client.call("GET", path)
            events.put(("segments", progress["segments"]))
            if progress["state"] == "succeeded":
                events.put(("ready", "Ready"))
                return
            if progress["state"] == "failed":
                events.put(("retry", ("Gateway could not finish this capture. Retry to reprocess the saved audio.", True)))
                return
            received = set(progress["received"])
            for fragment in ready:
                index = int(fragment.stem)
                if index in received:
                    continue
                with io.BytesIO() as buf:
                    with wave.open(buf, "wb") as wav:
                        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        data = fragment.read_bytes()
                        wav.writeframes(data[:len(data) // 2 * 2])
                    client.call("PUT", path + f"/fragments/{index}", data=buf.getvalue(),
                                headers={"Content-Type": "audio/wav"})
            if done and ready and not progress["closed"]:
                client.call("POST", path + "/finalize", json={"last_index": int(ready[-1].stem)})
            time.sleep(1)
        except requests.HTTPError as error:
            status = error.response.status_code
            if status in (429, 502, 503, 504):
                events.put(("notice", "Gateway busy or unavailable. Retrying saved fragments."))
                time.sleep(3)
                continue
            restart = status == 410 or (status == 404 and path is not None)
            events.put(("retry", (f"Gateway returned HTTP {status}. Saved audio is available to retry.", restart)))
            return
        except (requests.exceptions.SSLError, requests.exceptions.InvalidURL, ValueError, KeyError):
            events.put(("retry", ("Check the Gateway endpoint, TLS trust and configuration.", False)))
            return
        except (requests.Timeout, requests.ConnectionError):
            events.put(("notice", "Connection interrupted. Retrying saved fragments."))
            time.sleep(3)
        except (requests.RequestException, OSError):
            events.put(("retry", ("Could not sync saved audio. Check the client state and retry.", False)))
            return


def focused():
    # Keep a process-local AT-SPI object identity. Never synthesize keystrokes.
    try:
        import pyatspi
        match=pyatspi.findDescendant(pyatspi.Registry.getDesktop(0),
              lambda obj: obj.getState().contains(pyatspi.STATE_FOCUSED) and obj.getState().contains(pyatspi.STATE_EDITABLE))
        if match:
            return match
    except Exception:
        pass
    return None


def graphical():
    import tkinter as tk
    from pynput.keyboard import GlobalHotKeys
    cfg=json.loads(CONFIG.read_bytes())
    HOME.mkdir(mode=0o700,parents=True,exist_ok=True)
    pointer=HOME/"current"
    for old in HOME.iterdir():
        record=old/"editor.json"
        if record.is_file() and time.time()-record.stat().st_mtime>86400:
            if pointer.exists() and pointer.read_text()==old.name:
                pointer.unlink()
            for item in old.iterdir():
                if item.suffix in (".json",".raw"):
                    item.unlink()
            if not any(old.iterdir()):
                old.rmdir()
    root=tk.Tk()
    root.title("InnerVoice")
    status=tk.StringVar(value="Ready to record")
    tk.Label(root,textvariable=status).pack()
    editor=tk.Text(root,width=65,height=12,wrap="word")
    editor.pack()
    events=queue.Queue()
    directory=None
    state={}
    target=None
    stop=threading.Event()
    finished=threading.Event()
    working=False
    transfer_active = False
    replace_session = False

    def save():
        if directory:
            state["text"]=editor.get("1.0","end-1c")
            atomic(directory/"editor.json",json.dumps(state).encode())

    def changed(_):
        if editor.edit_modified():
            save()
            editor.edit_modified(False)
    editor.bind("<<Modified>>",changed)

    def transfer():
        nonlocal transfer_active
        transfer_active = True
        retry_button.configure(state="disabled")
        key = state.get("session_key", directory.name)
        threading.Thread(target=network, args=(cfg, directory, key, events, finished), daemon=True).start()

    def retry():
        nonlocal replace_session
        if transfer_active or not finished.is_set() or not directory or state.get("ready"):
            return
        if replace_session:
            state["session_key"] = secrets.token_hex(16)
            save()
            replace_session = False
        status.set("Syncing saved audio")
        transfer()

    def insert():
        if not directory or not state.get("ready") or state.get("intent") or target is None:
            return
        try:
            if focused()!=target:
                status.set("Focus changed. Copy text from the editor.")
                return
            text=editor.get("1.0","end-1c")
            pos=target.queryText().caretOffset
            state["intent"]="pending"
            save()
            if not target.queryEditableText().insertText(pos,text,len(text)):
                raise ValueError()
            state["intent"]="inserted"
            save()
            status.set("Inserted")
        except Exception:
            status.set("Insertion outcome unknown. Check the destination.")

    def toggle():
        nonlocal directory,state,working,target,stop,finished
        if working:
            if not stop.is_set():
                stop.set()
                status.set("Finishing recording")
            return
        if directory and not state.get("ready"):
            status.set("Recover the current recording first")
            return
        target=focused()
        directory=HOME/secrets.token_hex(16)
        directory.mkdir(mode=0o700)
        state={"applied":0,"ready":False}
        editor.delete("1.0","end")
        save()
        atomic(pointer,directory.name.encode())
        stop=threading.Event()
        finished=threading.Event()
        working=True
        status.set("Recording")
        threading.Thread(target=capture,args=(directory,stop,events,cfg["microphone"]),daemon=True).start()
        transfer()

    def poll():
        nonlocal working, transfer_active, replace_session
        while not events.empty():
            kind,value=events.get()
            if kind=="toggle":
                toggle()
            elif kind=="stopped":
                finished.set()
                if transfer_active:
                    status.set("Processing saved audio")
                else:
                    retry_button.configure(state="normal")
            elif kind=="segments":
                for part in value:
                    if part["index"]==state["applied"]:
                        if part["text"]:
                            editor.insert("end-1c",(" " if editor.get("1.0","end-1c") else "")+part["text"])
                        state["applied"]+=1
                        save()
            elif kind=="retry":
                transfer_active = False
                message, replace_session = value
                status.set(message)
                if finished.is_set():
                    retry_button.configure(state="normal")
            elif kind=="ready":
                transfer_active = False
                working=False
                state["ready"]=True
                save()
                status.set(value)
                retry_button.configure(state="disabled")
                for fragment in directory.glob("*.raw"):
                    try:
                        fragment.unlink(missing_ok=True)
                    except OSError:
                        status.set("Text saved. Local audio cleanup is pending.")
                insert()
            else:
                status.set(value)
        root.after(100,poll)

    def copy():
        root.clipboard_clear()
        root.clipboard_append(editor.get("1.0","end-1c"))
    tk.Button(root,text="Start / Stop",command=toggle).pack(side="left")
    tk.Button(root,text="Copy",command=copy).pack(side="right")
    retry_button = tk.Button(root, text="Retry sync", command=retry, state="disabled")
    retry_button.pack(side="left")
    if pointer.exists():
        directory=HOME/pointer.read_text()
        state=json.loads((directory/"editor.json").read_bytes())
        editor.insert("1.0",state.get("text",""))
        if state.get("intent")=="pending":
            status.set("Insertion outcome unknown. Check the destination.")
        elif not state["ready"]:
            working=True
            stop.set()
            finished.set()
            status.set("Recovering saved recording")
            transfer()
    from PIL import Image,ImageDraw
    import pystray
    icon=Image.new("RGBA",(64,64),(0,0,0,0))
    ImageDraw.Draw(icon).ellipse((8,8,56,56),fill="#287b94")
    tray=pystray.Icon("InnerVoice",icon,"InnerVoice",menu=pystray.Menu(
        pystray.MenuItem("Record / Stop",lambda *_:events.put(("toggle",None)))))
    threading.Thread(target=tray.run,daemon=True).start()
    status.trace_add("write",lambda *_:setattr(tray,"title","InnerVoice · "+status.get()))
    hotkeys=GlobalHotKeys({cfg["hotkey"]:lambda:events.put(("toggle",None))})
    hotkeys.start()
    poll()
    try:
        root.mainloop()
    finally:
        stop.set()
        hotkeys.stop()
        tray.stop()


def main():
    os.umask(0o077)
    action=sys.argv[1]
    if action=="setup":
        setup()
    elif action=="start":
        if not os.environ.get("DISPLAY"):
            sys.exit("Start in an X11 graphical session with microphone access.")
        cmd=["systemd-run","--user","--unit="+UNIT,"--collect"]
        for key in ("DISPLAY","XAUTHORITY","DBUS_SESSION_BUS_ADDRESS","XDG_RUNTIME_DIR","AT_SPI_BUS_ADDRESS"):
            if key in os.environ:
                cmd.append("--setenv="+key+"="+os.environ[key])
        subprocess.run(cmd+[sys.executable,str(Path(__file__).resolve()),"run"],check=True)
    elif action in ("stop","status"):
        subprocess.run(["systemctl","--user",action,UNIT],check=True)
    elif action=="run":
        graphical()
    else:
        sys.exit("desktop setup|start|status|stop")


if __name__=="__main__":
    main()
