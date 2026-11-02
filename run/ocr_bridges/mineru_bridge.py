import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

SUPPORTED_SUFFIXES = {'.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif', '.docx', '.pptx', '.xlsx'}


def collect_files(input_path: Path):
    if input_path.is_file():
        return [input_path]
    if input_path.is_dir():
        return [p for p in input_path.rglob('*') if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES]
    raise FileNotFoundError(f'Input path not found: {input_path}')


def main():
    parser = argparse.ArgumentParser(description='Submit a MinerU API task without downloading the result ZIP.')
    parser.add_argument('input_path', help='File or directory to parse')
    parser.add_argument('--api-url', default='http://127.0.0.1:8000')
    parser.add_argument('--backend', default='pipeline')
    parser.add_argument('--method', default='auto')
    parser.add_argument('--lang', default='ch')
    parser.add_argument('--effort', default='medium', choices=['medium', 'high'])
    parser.add_argument('--layered-pdf', action='store_true', help='Create a layered PDF after the task completes.')
    parser.add_argument('--poll-seconds', type=float, default=2.0)
    args = parser.parse_args()

    files_to_send = collect_files(Path(args.input_path).resolve())
    if not files_to_send:
        raise SystemExit('No supported files found.')

    data = {
        'backend': args.backend,
        'parse_method': args.method,
        'lang_list': args.lang,
        'effort': args.effort,
        'return_md': 'true',
        'return_middle_json': 'true',
        'return_model_output': 'true',
        'return_content_list': 'true',
        'return_images': 'false',
        'response_format_zip': 'false',
        'return_original_file': 'true',
    }

    opened = []
    try:
        multipart = []
        for path in files_to_send:
            fh = path.open('rb')
            opened.append(fh)
            multipart.append(('files', (path.name, fh, 'application/octet-stream')))

        response = requests.post(f'{args.api_url.rstrip("/")}/tasks', data=data, files=multipart, timeout=120)
        response.raise_for_status()
        task = response.json()
    finally:
        for fh in opened:
            fh.close()

    task_id = task['task_id']
    print(f'Task submitted: {task_id}')

    while True:
        status_response = requests.get(f'{args.api_url.rstrip("/")}/tasks/{task_id}', timeout=300)
        status_response.raise_for_status()
        status = status_response.json()
        state = status.get('status')
        if state in {'completed', 'failed'}:
            break
        queued = status.get('queued_ahead')
        suffix = f', queued_ahead={queued}' if queued is not None else ''
        print(f'Status: {state}{suffix}')
        time.sleep(args.poll_seconds)

    print(f'Status: {state}')
    if state != 'completed':
        print(status.get('error') or status, file=sys.stderr)
        raise SystemExit(1)

    output_root = Path(os.environ.get('MINERU_API_OUTPUT_ROOT', 'output'))
    output_dir = output_root / task_id
    print(f'Output directory: {output_dir.resolve()}')

    if args.layered_pdf:
        layered_tool = Path(__file__).with_name('make_layered_pdf.py')
        print('Creating layered PDF...')
        subprocess.run([sys.executable, str(layered_tool), str(output_dir)], check=True)


if __name__ == '__main__':
    main()