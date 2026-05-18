# Language detection + speech-friendly text processing
# Unicode range detection — no external API needed

import re


def detect_language(text: str) -> str:
    if not text or not text.strip():
        return 'en'

    script_map = {
        'ml': r'[ഀ-ൿ]',
        'hi': r'[ऀ-ॿ]',
        'ta': r'[஀-௿]',
        'te': r'[ఀ-౿]',
        'bn': r'[ঀ-৿]',
        'kn': r'[ಀ-೿]',
        'gu': r'[઀-૿]',
        'pa': r'[਀-੿]',
    }
    for lang, pattern in script_map.items():
        if re.search(pattern, text):
            lang_map = {'kn': 'kn', 'gu': 'hi', 'pa': 'hi'}
            return lang_map.get(lang, lang)

    hinglish_words = {
        'kya', 'hai', 'nahi', 'nhi', 'mujhe', 'chahiye', 'kitna',
        'kab', 'kahan', 'aur', 'bhi', 'mere', 'iska', 'uska',
        'yeh', 'woh', 'accha', 'theek', 'bilkul', 'haan', 'naa',
        'bhai', 'yaar', 'dost', 'karo', 'karein', 'lena', 'dena',
        'milega', 'milta', 'batao', 'dekho', 'sunlo', 'zyada',
        'thoda', 'bahut', 'sirf', 'abhi', 'kal', 'aaj',
        'kitne', 'wala', 'wali', 'wale', 'kr', 'hn',
    }
    words = set(re.sub(r'[^\w\s]', '', text.lower()).split())
    if len(words & hinglish_words) >= 2:
        return 'hi'

    ml_roman = {'ningal', 'njan', 'ente', 'avan', 'aval', 'ithu', 'ethu',
                'sheriyano', 'parayan', 'veno', 'cheyyuka', 'kandam',
                'swagatham', 'namaskaram', 'enkil'}
    if words & ml_roman:
        return 'ml'

    ta_roman = {'naan', 'nee', 'avan', 'ungal', 'enna', 'eppo',
                'enga', 'vanakkam', 'romba', 'sollu', 'paesu'}
    if words & ta_roman:
        return 'ta'

    return 'en'


def get_language_name(lang_code: str) -> str:
    return {
        'en': 'English', 'hi': 'Hindi', 'ml': 'Malayalam',
        'ta': 'Tamil',   'te': 'Telugu', 'bn': 'Bengali',
        'kn': 'Kannada', 'gu': 'Gujarati', 'pa': 'Punjabi',
    }.get(lang_code, 'English')


def make_speech_friendly(text: str, language: str = 'en') -> str:
    """Convert LLM response to natural spoken format before TTS."""
    if not text:
        return ''

    # Strip emojis
    text = re.sub(r'[\U00010000-\U0010FFFF\U0001F300-\U0001F9FF☀-⛿✀-➿︀-️]+', ' ', text)

    # Remove markdown
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*',     r'\1', text)
    text = re.sub(r'__(.+?)__',     r'\1', text)
    text = re.sub(r'_(.+?)_',       r'\1', text)
    text = re.sub(r'#{1,6}\s+',     '',    text)
    text = re.sub(r'`(.+?)`',       r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '',   text)

    # Convert bullet/numbered lists to natural speech
    lines = text.split('\n')
    list_items: list[str] = []
    non_list_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if re.match(r'^[\-•*]\s+', line):
            list_items.append(re.sub(r'^[\-•*]\s+', '', line))
        elif re.match(r'^\d+\.\s+', line):
            list_items.append(re.sub(r'^\d+\.\s+', '', line))
        else:
            if list_items:
                if len(list_items) == 1:
                    non_list_lines.append(list_items[0])
                elif len(list_items) == 2:
                    non_list_lines.append(f"{list_items[0]} and {list_items[1]}")
                else:
                    non_list_lines.append(', '.join(list_items[:-1]) + f', and {list_items[-1]}')
                list_items = []
            if line:
                non_list_lines.append(line)
    if list_items:
        if len(list_items) == 1:
            non_list_lines.append(list_items[0])
        elif len(list_items) == 2:
            non_list_lines.append(f"{list_items[0]} and {list_items[1]}")
        else:
            non_list_lines.append(', '.join(list_items[:-1]) + f', and {list_items[-1]}')
    text = ' '.join(non_list_lines)

    # Phone numbers → digit-by-digit
    def _phone_to_digits(m: re.Match) -> str:
        digits = re.sub(r'\D', '', m.group(0))
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        return ' '.join(digits)
    text = re.sub(r'\+91[\s\-]?[6-9]\d{9}', _phone_to_digits, text)
    text = re.sub(r'(?<!\d)[6-9]\d{9}(?!\d)', _phone_to_digits, text)
    text = re.sub(r'(?<!\d)\d{3,4}[\s\-]\d{3,4}[\s\-]\d{3,4}(?!\d)', _phone_to_digits, text)

    # Abbreviations
    text = re.sub(r'\be\.g\.', 'for example', text, flags=re.IGNORECASE)
    text = re.sub(r'\bi\.e\.', 'that is', text, flags=re.IGNORECASE)
    text = re.sub(r'\betc\.', 'and so on', text, flags=re.IGNORECASE)
    text = re.sub(r'\bvs\.', 'versus', text, flags=re.IGNORECASE)
    text = re.sub(r'\bft\.', 'feet', text, flags=re.IGNORECASE)
    text = re.sub(r'\bapprox\.', 'approximately', text, flags=re.IGNORECASE)
    text = re.sub(r'\bmin\.', 'minimum', text, flags=re.IGNORECASE)
    text = re.sub(r'\bmax\.', 'maximum', text, flags=re.IGNORECASE)
    text = re.sub(r'\bqty\.?', 'quantity', text, flags=re.IGNORECASE)
    text = re.sub(r'\bpcs\.?', 'pieces', text, flags=re.IGNORECASE)
    text = re.sub(r'\bno\.\s*(\d)', r'number \1', text, flags=re.IGNORECASE)

    # Symbols
    text = re.sub(r'\s*&\s*', ' and ', text)
    text = re.sub(r'(\d)\s*%', r'\1 percent', text)
    text = re.sub(r'\s*\+\s*', ' plus ', text)
    text = re.sub(r'\s*[—–]\s*', ', ', text)
    text = re.sub(r'\s*/\s*', ' or ', text)
    text = re.sub(r'\(([^)]{1,60})\)', r', \1,', text)
    text = re.sub(r'\[([^\]]{1,60})\]', r', \1,', text)

    # Currency
    if language in ('en', 'hi', 'ml', 'ta', 'te', 'bn'):
        text = re.sub(
            r'₹\s*([\d,]+(?:\.\d{1,2})?)',
            lambda m: m.group(1).replace(',', '') + ' rupees',
            text,
        )
        text = re.sub(
            r'\$\s*([\d,]+)(?:\.(\d{2}))?',
            lambda m: m.group(1).replace(',', '') + ' dollars' +
                      (f" {m.group(2)} cents" if m.group(2) and m.group(2) != '00' else ''),
            text,
        )

    # Clean up
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()

    # Break long sentences
    def _break_long(sentence: str) -> str:
        if len(sentence.split()) <= 25:
            return sentence
        for conj in [' and ', ' but ', ' so ', ' because ', ' which ', ' that ', ' however ']:
            idx = sentence.lower().find(conj, len(sentence) // 3)
            if idx != -1:
                left = sentence[:idx].rstrip()
                right = sentence[idx + len(conj):].strip()
                if right:
                    return left + '. ' + right[0].upper() + right[1:]
        return sentence

    text = ' '.join(_break_long(s) for s in re.split(r'(?<=[.!?])\s+', text) if s.strip())

    # Trim to 200 words
    wds = text.split()
    if len(wds) > 200:
        short = ' '.join(wds[:200])
        cutoff = max(short.rfind('. '), short.rfind('! '), short.rfind('? '))
        text = short[:cutoff + 1] if cutoff > 60 else short + '.'

    return text.strip()


AZURE_VOICE_MAP = {
    'en': 'en-IN-NeerjaNeural', 'hi': 'hi-IN-SwaraNeural',
    'ml': 'ml-IN-SobhanaNeural', 'ta': 'ta-IN-PallaviNeural',
    'te': 'te-IN-ShrutiNeural', 'bn': 'bn-IN-TanishaaNeural',
    'kn': 'kn-IN-SapnaNeural', 'gu': 'gu-IN-DhwaniNeural',
    'pa': 'pa-IN-OjaswanthNeural',
}

def get_azure_voice(language: str) -> str:
    return AZURE_VOICE_MAP.get(language, AZURE_VOICE_MAP['en'])

BROWSER_LANG_MAP = {
    'en': 'en-IN', 'hi': 'hi-IN', 'ml': 'ml-IN',
    'ta': 'ta-IN', 'te': 'te-IN', 'bn': 'bn-BD',
    'kn': 'kn-IN', 'gu': 'gu-IN', 'pa': 'pa-IN',
}

def get_browser_lang(language: str) -> str:
    return BROWSER_LANG_MAP.get(language, 'en-IN')
