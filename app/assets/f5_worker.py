"""F5-TTS worker, run with the local voice's own Python (localvoice.py): loads the model once, then reads one JSON job
per line on stdin and answers one JSON line on the original stdout. Everything the libraries print goes to stderr.

Job: {"ref": wav path, "ref_text": its words, "text": what to say, "out": wav path, "speed": 1.0, "steps": 32}
"""
import json
import os
import sys

proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)  # print() and progress bars from here on land on stderr


def answer(**kw):
    proto.write(json.dumps(kw) + "\n")


def main():
    model_dir, hf_cache = sys.argv[1], sys.argv[2]
    try:
        from f5_tts.api import F5TTS

        tts = F5TTS(model="F5TTS_Base", ckpt_file=os.path.join(model_dir, "model.safetensors"),
                    vocab_file=os.path.join(model_dir, "vocab.txt"), hf_cache_dir=hf_cache)
    except Exception as e:  # noqa: BLE001 — reported to the app
        answer(ok=False, error=f"Could not load the voice model: {e.__class__.__name__}: {e}"[:500])
        return
    answer(ok=True, ready=True, device=str(tts.device))
    for line in sys.stdin:
        try:
            job = json.loads(line)
            tts.infer(job["ref"], job["ref_text"], job["text"], show_info=lambda *a: None, speed=float(job.get("speed", 1.0)),
                      nfe_step=int(job.get("steps", 32)), file_wave=job["out"], seed=int(job.get("seed", 7)))
            answer(ok=True)
        except Exception as e:  # noqa: BLE001
            answer(ok=False, error=f"{e.__class__.__name__}: {e}"[:500])


if __name__ == "__main__":
    main()
