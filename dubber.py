import argparse
import os
import asyncio
import subprocess
from pathlib import Path

# Add current directory to PATH so local ffmpeg/ffprobe can be found by pydub and others
os.environ["PATH"] += os.pathsep + os.path.abspath(os.path.dirname(__file__))

import yt_dlp
import whisper
import edge_tts
from pydub import AudioSegment
from tqdm import tqdm

def download_video(url, output_dir):
    print(f"Downloading video from {url}...")
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': os.path.join(output_dir, 'video.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    
    video_path = os.path.join(output_dir, 'video.mp4')
    audio_path = os.path.join(output_dir, 'original_audio.wav')
    
    print("Extracting audio for transcription...")
    subprocess.run(['ffmpeg', '-y', '-i', video_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', audio_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return video_path, audio_path

def transcribe_and_translate(audio_path, model_size="base"):
    print(f"Loading Whisper '{model_size}' model...")
    model = whisper.load_model(model_size)
    print("Transcribing and translating to English...")
    result = model.transcribe(audio_path, task="translate")
    return result['segments']

async def synthesize_segment(text, output_path, voice="en-US-ChristopherNeural", retries=3, delay=2):
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)
            return True
        except Exception as e:
            if attempt < retries - 1:
                print(f"\nWarning: TTS failed for segment (Attempt {attempt+1}/{retries}). Retrying in {delay}s... Error: {e}")
                await asyncio.sleep(delay)
            else:
                print(f"\nError: TTS failed completely after {retries} attempts: {e}")
                raise e

async def synthesize_all(segments, output_dir):
    print("Synthesizing English speech for each segment...")
    tts_dir = os.path.join(output_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)
    
    tasks = []
    for i, segment in enumerate(segments):
        text = segment['text'].strip()
        if not text:
            continue
        out_path = os.path.join(tts_dir, f"{i}.mp3")
        duration = segment['end'] - segment['start']
        tasks.append((i, segment['start'], duration, out_path, text))
    
    successful_tasks = []
    for i, start, dur, path, text in tqdm(tasks, desc="TTS Generation"):
        try:
            success = await synthesize_segment(text, path)
            if success:
                successful_tasks.append((i, start, dur, path))
        except Exception as e:
            print(f"\nSkipping segment {i} ('{text[:30]}...') due to persistent synthesis failure.")
            
    return successful_tasks

def combine_audio(original_audio_path, tts_tasks, output_path, keep_background=True, background_volume_db=-18):
    print("Overlaying synthesized speech onto a new audio track...")
    original = AudioSegment.from_wav(original_audio_path)
    
    if keep_background:
        # Keep original audio as background, but lowered in volume
        dubbed = original + background_volume_db
    else:
        # Create silent audio of same length
        dubbed = AudioSegment.silent(duration=len(original))
    
    for idx, start_time, duration_sec, audio_file in tqdm(tts_tasks, desc="Overlaying audio"):
        if not os.path.exists(audio_file):
            continue
        try:
            segment_audio = AudioSegment.from_file(audio_file)
            tts_duration_sec = len(segment_audio) / 1000.0
            
            # If TTS is longer than original segment duration, speed it up to fit
            if tts_duration_sec > duration_sec and duration_sec > 0:
                speed_ratio = tts_duration_sec / duration_sec
                # Cap speedup to 2.0x so it remains intelligible
                if speed_ratio > 2.0:
                    speed_ratio = 2.0
                
                # Only apply speedup if > 1.0 (pydub's speedup bug guard)
                if speed_ratio > 1.0:
                    from pydub.effects import speedup
                    segment_audio = speedup(segment_audio, speed_ratio)
            
            start_ms = int(start_time * 1000)
            end_ms = start_ms + len(segment_audio)
            
            # Extend dubbed audio track if segment overruns original length
            if end_ms > len(dubbed):
                padding = AudioSegment.silent(duration=end_ms - len(dubbed))
                dubbed = dubbed + padding
                
            dubbed = dubbed.overlay(segment_audio, position=start_ms)
        except Exception as e:
            print(f"Failed to overlay segment {idx}: {e}")
            
    print("Exporting dubbed audio track...")
    dubbed.export(output_path, format="wav")

def mux_video_audio(video_path, dubbed_audio_path, output_path):
    print(f"Muxing video and dubbed audio into {output_path}...")
    subprocess.run([
        'ffmpeg', '-y', '-i', video_path, '-i', dubbed_audio_path,
        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
        '-shortest',  # Cut output when video stream ends
        output_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Done!")

async def main():
    parser = argparse.ArgumentParser(description="Automated Video Dubbing System")
    parser.add_argument("url", help="YouTube URL to dub")
    parser.add_argument("--outdir", default="output", help="Output directory")
    parser.add_argument("--keep-bg", action="store_true", help="Keep original audio in background")
    args = parser.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    
    try:
        video_path, audio_path = download_video(args.url, args.outdir)
        segments = transcribe_and_translate(audio_path, model_size="base")
        tts_tasks = await synthesize_all(segments, args.outdir)
        
        dubbed_audio_path = os.path.join(args.outdir, 'dubbed_audio.wav')
        combine_audio(audio_path, tts_tasks, dubbed_audio_path, keep_background=args.keep_bg)
        
        final_video_path = os.path.join(args.outdir, 'final_dubbed.mp4')
        mux_video_audio(video_path, dubbed_audio_path, final_video_path)
        
        print(f"\nSuccessfully dubbed video saved to {final_video_path}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())
