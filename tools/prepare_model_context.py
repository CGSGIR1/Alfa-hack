"""Create a separate, ignored Docker build context; never edit the source model."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import shutil
import urllib.request


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'li', 'h1', 'h2', 'h3', 'br'):
            self.parts.append('\n')
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if href.startswith('https://ai.google.dev/gemma/'):
                self.parts.append(' [' + href + '] ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden -= 1

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, target = args.model.resolve(), args.output.resolve()
    if not source.is_dir() or not (source / 'model.safetensors').is_file():
        parser.error('A complete local embeddinggemma-300m directory is required')
    if target.exists() or target == source or target.is_relative_to(source):
        parser.error('Output must be a new separate directory')
    # Download before copying: network errors leave no half-copied model context.
    terms = []
    for url in ('https://ai.google.dev/gemma/terms', 'https://ai.google.dev/gemma/prohibited_use_policy'):
        with urllib.request.urlopen(url, timeout=30) as response:
            html = response.read().decode('utf-8')
        extractor = Text(); extractor.feed(html)
        terms.append('Source: ' + url + '\n' + ''.join(extractor.parts))
    agreement = '\n\n'.join(terms)
    if 'Distribution and Redistribution' not in agreement:
        parser.error('Could not verify the downloaded Gemma agreement; inspect the official source')
    target.mkdir(parents=True)
    shutil.copytree(source, target / 'embeddinggemma-300m',
                    ignore=shutil.ignore_patterns('.git', '.cache', '.env', '*.lock'))
    (target / 'Gemma-Terms.txt').write_text(agreement, encoding='utf-8')
    (target / 'Gemma-Notice.txt').write_text(
        'Gemma is provided under and subject to the Gemma Terms of Use found at ai.google.dev/gemma/terms\n', encoding='utf-8')
    print('Model context prepared: ' + str(target))


if __name__ == '__main__':
    main()
