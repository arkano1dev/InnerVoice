#!/usr/bin/env python3
"""Ubuntu graphical client. Capture, network, and UI use independent threads."""
import argparse
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


def network(cfg,directory,events,finished):
    client=Client(cfg)
    key=directory.name
    while True:
        try:
            session=client.call("POST","/v1/sessions",headers={"Idempotency-Key":key})["session_id"]
            path="/v1/sessions/"+session
            progress=client.call("GET",path)
            files=sorted(directory.glob("*.raw"))
            done=finished.is_set()
            ready=[p for p in files if p.stat().st_size==BLOCK or (done and p.stat().st_size>=2)]
            for p in ready:
                idx=int(p.stem)
                if idx in progress["received"]:
                    continue
                buf=io.BytesIO()
                with wave.open(buf,"wb") as w:
                    w.setparams((1,2,16000,0,"NONE","not compressed"))
                    data=p.read_bytes()
                    w.writeframes(data[:len(data)//2*2])
                client.call("PUT",path+f"/fragments/{idx}",data=buf.getvalue(),headers={"Content-Type":"audio/wav"})
            events.put(("segments",progress["segments"]))
            if progress["state"]=="succeeded":
                for p in files:
                    p.unlink(missing_ok=True)
                events.put(("ready","Ready"))
                return
            if done and not ready:
                events.put(("ready","Empty recording"))
                return
            if done and not progress["closed"]:
                client.call("POST",path+"/finalize",json={"last_index":int(ready[-1].stem)})
            if progress["state"]=="failed":
                events.put(("notice","Gateway reported a failed capture."))
                return
            time.sleep(1)
        except Exception as error:
            response=getattr(error,"response",None)
            if response is not None and response.status_code in (400,401,404,409,410,413,415,422):
                events.put(("notice","Gateway rejected capture: "+str(response.status_code)))
                return
            events.put(("notice","Gateway unavailable. Retrying saved fragments."))
            time.sleep(3)


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
        threading.Thread(target=network,args=(cfg,directory,events,finished),daemon=True).start()

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
        nonlocal working
        while not events.empty():
            kind,value=events.get()
            if kind=="toggle":
                toggle()
            elif kind=="stopped":
                finished.set()
                status.set("Processing saved audio")
            elif kind=="segments":
                for part in value:
                    if part["index"]==state["applied"]:
                        if part["text"]:
                            editor.insert("end-1c",(" " if editor.get("1.0","end-1c") else "")+part["text"])
                        state["applied"]+=1
                        save()
            elif kind=="ready":
                working=False
                state["ready"]=True
                save()
                status.set(value)
                insert()
            else:
                status.set(value)
        root.after(100,poll)

    def copy():
        root.clipboard_clear()
        root.clipboard_append(editor.get("1.0","end-1c"))
    tk.Button(root,text="Start / Stop",command=toggle).pack(side="left")
    tk.Button(root,text="Copy",command=copy).pack(side="right")
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
