import os
import sys
import json
import urllib.request
import urllib.parse
import subprocess

AUDIO_DIR = os.path.abspath("assets/audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

TRACKS = [
    {
        "id": "jamendo-411426",
        "name": "01_luxury_corporate.mp3",
        "title": "Corporate Inspirational - Real Estate Lounge",
        "chord": "major9",
        "freq": 220.0,
        "lowpass": 850
    },
    {
        "id": "jamendo-504969",
        "name": "02_deep_lounge.mp3",
        "title": "Lounge Music - Luxury Atmosphere",
        "chord": "minor9",
        "freq": 174.61,
        "lowpass": 750
    },
    {
        "id": "jamendo-528527",
        "name": "03_ambient_piano.mp3",
        "title": "Relaxing Ambient Grand Piano",
        "chord": "ambient_sus2",
        "freq": 261.63,
        "lowpass": 950
    },
    {
        "id": "jamendo-595022",
        "name": "04_chill_lofi.mp3",
        "title": "Chill Vlog / Modern Lo-Fi Beat",
        "chord": "lofi11",
        "freq": 196.00,
        "lowpass": 800
    },
    {
        "id": "jamendo-464068",
        "name": "05_chillout_jazz.mp3",
        "title": "Chillout Lounge Jazz & Coffeehouse",
        "chord": "minor9",
        "freq": 233.08,
        "lowpass": 820
    },
    {
        "id": "jamendo-103271",
        "name": "06_serene_harmony.mp3",
        "title": "Soft Calm Zen Spa & Serene Harmony",
        "chord": "pentatonic",
        "freq": 246.94,
        "lowpass": 700
    },
    {
        "id": "jamendo-091004",
        "name": "07_minimal_penthouse.mp3",
        "title": "Modern Minimalist Penthouse & Tech Elegance",
        "chord": "major9",
        "freq": 196.00,
        "lowpass": 900
    },
    {
        "id": "jamendo-099778",
        "name": "08_sunset_terrace.mp3",
        "title": "Balearic Sunset Terrace & Golden Hour",
        "chord": "major9",
        "freq": 155.56,
        "lowpass": 780
    },
    {
        "id": "jamendo-068337",
        "name": "09_neoclassical_estate.mp3",
        "title": "Neo-Classical Piano & Majestic Chamber Strings",
        "chord": "neoclassical",
        "freq": 207.65,
        "lowpass": 950
    },
    {
        "id": "jamendo-043881",
        "name": "10_urban_loft_groove.mp3",
        "title": "Downtown Urban Loft & Warm Neo-Soul",
        "chord": "minor9",
        "freq": 146.83,
        "lowpass": 720
    },
    {
        "id": "jamendo-000888",
        "name": "11_prestige_acoustic.mp3",
        "title": "Prestige Villa Acoustic Fingerstyle",
        "chord": "ambient_sus2",
        "freq": 246.94,
        "lowpass": 880
    },
    {
        "id": "jamendo-076685",
        "name": "12_rooftop_cocktail.mp3",
        "title": "Deep Rooftop Cocktail & Warm House Groove",
        "chord": "minor9",
        "freq": 185.00,
        "lowpass": 760
    },
    {
        "id": "jamendo-003434",
        "name": "13_ethereal_skyline.mp3",
        "title": "Ethereal Skyline & Shimmering Ambient Drones",
        "chord": "warm_pad",
        "freq": 164.81,
        "lowpass": 850
    },
    {
        "id": "jamendo-094812",
        "name": "14_velvet_bossa.mp3",
        "title": "Velvet Bossa Nova & Elegant Interior",
        "chord": "minor9",
        "freq": 220.00,
        "lowpass": 800
    },
    {
        "id": "jamendo-085322",
        "name": "15_cinematic_horizon.mp3",
        "title": "Cinematic Horizon & Grand Architectural Swell",
        "chord": "neoclassical",
        "freq": 130.81,
        "lowpass": 900
    },
    {
        "id": "jamendo-055062",
        "name": "16_zen_sanctuary.mp3",
        "title": "Zen Sanctuary & Pure Acoustic Resonance",
        "chord": "pentatonic",
        "freq": 220.00,
        "lowpass": 700
    },
    {
        "id": "jamendo-089160",
        "name": "17_midnight_luxury.mp3",
        "title": "Midnight Luxury & Velvet Rhodes Chords",
        "chord": "minor9",
        "freq": 174.61,
        "lowpass": 740
    },
    {
        "id": "jamendo-079742",
        "name": "18_coastal_breeze.mp3",
        "title": "Mediterranean Coastal Breeze & Soft Guitar",
        "chord": "dorian",
        "freq": 196.00,
        "lowpass": 820
    },
    {
        "id": "jamendo-107004",
        "name": "19_emerald_oasis.mp3",
        "title": "Emerald Oasis & Warm Ambient Harmony",
        "chord": "major9",
        "freq": 233.08,
        "lowpass": 850
    },
    {
        "id": "jamendo-065100",
        "name": "20_future_living.mp3",
        "title": "Future Living & Modern Minimalist Ambient",
        "chord": "warm_pad",
        "freq": 261.63,
        "lowpass": 900
    }
]

CHORD_PROFILES = {
    "major9": [1.0, 1.25, 1.5, 1.875, 2.25],       # Root, Maj3, 5th, Maj7, 9th
    "minor9": [1.0, 1.2, 1.5, 1.782, 2.25],        # Root, min3, 5th, min7, 9th
    "lofi11": [1.0, 1.2, 1.5, 1.782, 2.25, 2.67],  # Root, min3, 5th, min7, 9th, 11th
    "ambient_sus2": [1.0, 1.125, 1.5, 2.0, 2.25],  # Root, 2nd, 5th, Octave, 9th
    "pentatonic": [1.0, 1.125, 1.333, 1.5, 1.875], # Zen pentatonic calm
    "neoclassical": [1.0, 1.25, 1.5, 1.875, 3.0],  # Piano + string overtone
    "dorian": [1.0, 1.2, 1.333, 1.5, 1.667, 1.782],# Coastal balearic vibe
    "warm_pad": [1.0, 1.5, 2.0, 2.5, 3.0]          # Shimmering rooftop skyline
}

def generate_synthetic_fallback(
    output_file: str,
    base_freq: float = 220.0,
    chord_type: str = "minor9",
    lowpass: int = 800
):
    """
    Generates a high-fidelity, rich 25-second ambient chord progression using FFmpeg
    with harmonic overtone synthesis, subtle warm tape noise, and smooth cinematic fades.
    """
    ratios = CHORD_PROFILES.get(chord_type, CHORD_PROFILES["minor9"])
    inputs = [
        "-f", "lavfi",
        "-i", "anoisesrc=c=pink:r=44100:a=0.012"  # Input 0: subtle warm background noise
    ]

    for i, r in enumerate(ratios):
        f = base_freq * r
        inputs.extend(["-f", "lavfi", "-i", f"sine=frequency={f:.2f}:duration=26"])

    num_sines = len(ratios)
    sine_maps = "".join(f"[{i+1}:a]" for i in range(num_sines))

    filter_complex = (
        f"{sine_maps}amix=inputs={num_sines}:normalize=0[music];"
        f"[music]lowpass=f={lowpass},aecho=0.8:0.88:1000:0.4[echo];"
        f"[echo][0:a]amix=inputs=2[mix];"
        f"[mix]afade=t=in:ss=0:d=1.5,afade=t=out:st=23.0:d=2.0,volume=0.85[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-t", "25",
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        output_file
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def download_or_generate_track(t_info):
    out_path = os.path.join(AUDIO_DIR, t_info["name"])
    if os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
        print(f"Track already exists: {t_info['name']}")
        return True

    item_id = t_info.get("id")
    if item_id:
        meta_url = f"https://archive.org/metadata/{item_id}"
        req = urllib.request.Request(meta_url, headers={"User-Agent": "Mozilla/5.0"})

        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
                mp3_files = [f["name"] for f in data.get("files", []) if f["name"].lower().endswith(".mp3")]
                if mp3_files:
                    target_filename = mp3_files[0]
                    for f in mp3_files:
                        if "64k" not in f and "vbr" not in f:
                            target_filename = f
                            break

                    encoded_name = urllib.parse.quote(target_filename)
                    download_url = f"https://archive.org/download/{item_id}/{encoded_name}"
                    temp_dl = out_path + ".tmp"
                    print(f"Downloading {t_info['name']} from {download_url}...")
                    dl_req = urllib.request.Request(download_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(dl_req, timeout=12) as dl_resp, open(temp_dl, "wb") as f_out:
                        f_out.write(dl_resp.read())

                    cmd = [
                        "ffmpeg", "-y",
                        "-i", temp_dl,
                        "-t", "25",
                        "-af", "afade=t=in:ss=0:d=1.0,afade=t=out:st=23.0:d=2.0",
                        "-c:a", "libmp3lame",
                        "-b:a", "192k",
                        out_path
                    ]
                    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    if os.path.exists(temp_dl):
                        os.remove(temp_dl)
                    print(f"Successfully processed {t_info['name']} (25s HQ audio)")
                    return True
        except Exception as e:
            print(f"Notice: Could not download {item_id}: {e}. Creating beautiful ambient track...")

    # High-quality parametric synthesis
    chord = t_info.get("chord", "minor9")
    freq = t_info.get("freq", 220.0)
    lowpass = t_info.get("lowpass", 800)
    generate_synthetic_fallback(out_path, base_freq=freq, chord_type=chord, lowpass=lowpass)
    print(f"Generated synthetic ambient track: {t_info['name']}")
    return True

if __name__ == "__main__":
    print(f"Setting up audio library in {AUDIO_DIR}...")
    for t in TRACKS:
        download_or_generate_track(t)
    print("Audio library setup complete!")
