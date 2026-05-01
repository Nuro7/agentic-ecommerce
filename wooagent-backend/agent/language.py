# agent/language.py
# Language detection + speech-friendly text processing
# No external API needed — instant Unicode range detection

import re
from typing import Optional

# ── Language detection ─────────────────────────────────────────────────────
def detect_language(text: str) -> str:
    """
    Detect language from user input using Unicode ranges.
    Returns: 'en' | 'hi' | 'ml' | 'ta' | 'te' | 'bn'
    
    Priority order matters — check scripts first, then Hinglish words.
    Falls back to 'en' if nothing matches.
    """
    if not text or not text.strip():
        return 'en'
    
    # Unicode script ranges — check these first (fastest, most accurate)
    script_map = {
        'ml': r'[\u0D00-\u0D7F]',  # Malayalam
        'hi': r'[\u0900-\u097F]',  # Devanagari (Hindi, Marathi)
        'ta': r'[\u0B80-\u0BFF]',  # Tamil
        'te': r'[\u0C00-\u0C7F]',  # Telugu
        'bn': r'[\u0980-\u09FF]',  # Bengali
        'kn': r'[\u0C80-\u0CFF]',  # Kannada
        'gu': r'[\u0A80-\u0AFF]',  # Gujarati
        'pa': r'[\u0A00-\u0A7F]',  # Punjabi/Gurmukhi
    }
    
    for lang, pattern in script_map.items():
        if re.search(pattern, text):
            # Map regional scripts to closest supported language
            lang_map = {'kn': 'kn', 'gu': 'hi', 'pa': 'hi'}
            return lang_map.get(lang, lang)
    
    # Hinglish detection — Hindi words written in Latin script
    hinglish_words = {
        'kya', 'hai', 'nahi', 'nhi', 'mujhe', 'chahiye', 'kitna',
        'kab', 'kahan', 'aur', 'bhi', 'mere', 'iska', 'uska',
        'yeh', 'woh', 'accha', 'theek', 'bilkul', 'haan', 'naa',
        'bhai', 'yaar', 'dost', 'karo', 'karein', 'lena', 'dena',
        'milega', 'milta', 'batao', 'dekho', 'sunlo', 'zyada',
        'thoda', 'bahut', 'sirf', 'abhi', 'kal', 'aaj',
        'kitne', 'wala', 'wali', 'wale', 'kr', 'hn'
        # Removed: 'price', 'h', 'ho' — too common in English, cause false positives
    }

    words = set(re.sub(r'[^\w\s]', '', text.lower()).split())
    hinglish_matches = len(words & hinglish_words)

    # Require at least 2 matches to avoid false positives on short English queries
    if hinglish_matches >= 2:
        return 'hi'
    
    # Malayalam romanized detection
    ml_roman = {'ningal', 'njan', 'ente', 'avan', 'aval', 'ithu',
                'ethu', 'sheriyano', 'parayan', 'veno', 'cheyyuka',
                'kandam', 'swagatham', 'namaskaram', 'enkil'}
    if words & ml_roman:
        return 'ml'
    
    # Tamil romanized
    ta_roman = {'naan', 'nee', 'avan', 'ungal', 'enna', 'eppo',
                'enga', 'vanakkam', 'romba', 'sollu', 'paesu'}
    if words & ta_roman:
        return 'ta'
    
    return 'en'


def get_language_name(lang_code: str) -> str:
    """Human-readable language name for logging."""
    return {
        'en': 'English', 'hi': 'Hindi', 'ml': 'Malayalam',
        'ta': 'Tamil',   'te': 'Telugu', 'bn': 'Bengali',
        'kn': 'Kannada', 'gu': 'Gujarati', 'pa': 'Punjabi'
    }.get(lang_code, 'English')


# ── Speech-friendly text processing ───────────────────────────────────────
def make_speech_friendly(text: str, language: str = 'en') -> str:
    """
    Convert LLM response text to natural spoken format.
    
    THIS IS CRITICAL. Without this:
    - "**Nike shoes** — ₹2,999" becomes "asterisk asterisk Nike shoes..."
    - Bullet points get spoken literally
    - Responses are too long to listen to
    
    Apply BEFORE sending to TTS.
    """
    if not text:
        return ''

    # 0. Strip emojis and symbols that TTS reads as "emoji" or gibberish
    text = re.sub(
        r'[\U00010000-\U0010FFFF'    # supplementary planes (most emojis)
        r'\U0001F300-\U0001F9FF'     # misc symbols & pictographs
        r'\u2600-\u26FF'             # misc symbols
        r'\u2700-\u27BF'             # dingbats
        r'\uFE00-\uFE0F'             # variation selectors
        r']+',
        ' ', text
    )

    # 1. Remove all markdown formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)   # **bold**
    text = re.sub(r'\*(.+?)\*',     r'\1', text)    # *italic*
    text = re.sub(r'__(.+?)__',     r'\1', text)    # __bold__
    text = re.sub(r'_(.+?)_',       r'\1', text)    # _italic_
    text = re.sub(r'#{1,6}\s+',     '',    text)    # # headers
    text = re.sub(r'`(.+?)`',       r'\1', text)    # `code`
    text = re.sub(r'```[\s\S]*?```','',    text)    # ```code blocks```
    
    # 2. Convert bullet/numbered lists to natural speech
    # "• Nike shoes\n• Adidas" → "Nike shoes and Adidas"
    lines = text.split('\n')
    list_items = []
    non_list_lines = []
    
    for line in lines:
        line = line.strip()
        if re.match(r'^[\-•*]\s+', line):
            list_items.append(re.sub(r'^[\-•*]\s+', '', line))
        elif re.match(r'^\d+\.\s+', line):
            list_items.append(re.sub(r'^\d+\.\s+', '', line))
        else:
            if list_items:
                # Convert accumulated list to spoken sentence
                if len(list_items) == 1:
                    non_list_lines.append(list_items[0])
                elif len(list_items) == 2:
                    non_list_lines.append(f"{list_items[0]} and {list_items[1]}")
                else:
                    all_but_last = ', '.join(list_items[:-1])
                    non_list_lines.append(f"{all_but_last}, and {list_items[-1]}")
                list_items = []
            if line:
                non_list_lines.append(line)
    
    # Handle trailing list items
    if list_items:
        if len(list_items) == 1:
            non_list_lines.append(list_items[0])
        elif len(list_items) == 2:
            non_list_lines.append(f"{list_items[0]} and {list_items[1]}")
        else:
            all_but_last = ', '.join(list_items[:-1])
            non_list_lines.append(f"{all_but_last}, and {list_items[-1]}")
    
    text = ' '.join(non_list_lines)
    
    # 3. Convert phone numbers to digit-by-digit reading BEFORE number processing
    # Indian mobile: 10 digits starting with 6-9 (optionally +91 prefix)
    # e.g. "9876543210" → "9 8 7 6 5 4 3 2 1 0"
    # e.g. "+91 9876543210" → "9 8 7 6 5 4 3 2 1 0"
    def _phone_to_digits(m: re.Match) -> str:
        digits = re.sub(r'\D', '', m.group(0))
        # Strip leading 91 country code if 12 digits
        if len(digits) == 12 and digits.startswith('91'):
            digits = digits[2:]
        return ' '.join(digits)

    # +91-XXXXXXXXXX or +91 XXXXXXXXXX
    text = re.sub(
        r'\+91[\s\-]?[6-9]\d{9}',
        _phone_to_digits, text
    )
    # Standalone 10-digit Indian mobile number
    text = re.sub(
        r'(?<!\d)[6-9]\d{9}(?!\d)',
        _phone_to_digits, text
    )
    # Any other phone-like patterns (7+ digits with dashes/spaces)
    text = re.sub(
        r'(?<!\d)\d{3,4}[\s\-]\d{3,4}[\s\-]\d{3,4}(?!\d)',
        _phone_to_digits, text
    )

    # 3b. Expand common abbreviations and symbols that TTS mispronounces
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

    # Symbols → spoken words
    text = re.sub(r'\s*&\s*', ' and ', text)       # & → and
    text = re.sub(r'(\d)\s*%', r'\1 percent', text) # 20% → 20 percent
    text = re.sub(r'\s*\+\s*', ' plus ', text)      # + → plus (between words)
    text = re.sub(r'\s*—\s*', ', ', text)            # em-dash → comma pause
    text = re.sub(r'\s*–\s*', ', ', text)            # en-dash → comma pause
    text = re.sub(r'\s*/\s*', ' or ', text)          # x/y → x or y (e.g. "cash/card")
    text = re.sub(r'\(([^)]{1,60})\)', r', \1,', text)  # (note) → , note, (natural aside)
    text = re.sub(r'\[([^\]]{1,60})\]', r', \1,', text) # [note] → , note,

    # 4. Convert currency symbols to spoken words
    if language in ('en', 'hi', 'ml', 'ta', 'te', 'bn'):
        # ₹2,999 → "2999 rupees"
        text = re.sub(
            r'₹\s*([\d,]+(?:\.\d{1,2})?)',
            lambda m: m.group(1).replace(',', '') + ' rupees',
            text
        )
        # $29.99 → "29 dollars 99 cents"
        text = re.sub(
            r'\$\s*([\d,]+)(?:\.(\d{2}))?',
            lambda m: (
                m.group(1).replace(',', '') + ' dollars' +
                (f" {m.group(2)} cents" if m.group(2) and m.group(2) != '00' else '')
            ),
            text
        )
    
    # 5. Remove URLs completely
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)

    # 6. Remove HTML tags if any leaked through
    text = re.sub(r'<[^>]+>', '', text)

    # 7. Fix whitespace
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+',  ' ', text)
    text = text.strip()

    # 8. Break very long sentences into shorter ones (helps TTS sound natural)
    # Sentences over ~25 words tend to be delivered in a flat, robotic tone
    def _break_long_sentence(sentence: str) -> str:
        words = sentence.split()
        if len(words) <= 25:
            return sentence
        # Try to split at natural conjunctions
        for conj in [' and ', ' but ', ' so ', ' because ', ' which ', ' that ', ' however ']:
            idx = sentence.lower().find(conj, len(sentence) // 3)  # after first third
            if idx != -1:
                left  = sentence[:idx].rstrip()
                right = sentence[idx + len(conj):].strip()
                if right:
                    return left + '. ' + right[0].upper() + right[1:]
        return sentence

    sentences = re.split(r'(?<=[.!?])\s+', text)
    text = ' '.join(_break_long_sentence(s) for s in sentences if s.strip())

    # 9. Trim to 200 words — enough for a full product description on a call
    words = text.split()
    if len(words) > 200:
        short  = ' '.join(words[:200])
        cutoff = max(
            short.rfind('. '),
            short.rfind('! '),
            short.rfind('? ')
        )
        if cutoff > 60:
            text = short[:cutoff + 1]
        else:
            text = short + '.'

    return text.strip()


# ── Azure TTS voice map ────────────────────────────────────────────────────
AZURE_VOICE_MAP = {
    'en': 'en-IN-NeerjaNeural',       # Indian English — warm, clear
    'hi': 'hi-IN-SwaraNeural',        # Hindi — natural, warm
    'ml': 'ml-IN-SobhanaNeural',      # Malayalam
    'ta': 'ta-IN-PallaviNeural',      # Tamil
    'te': 'te-IN-ShrutiNeural',       # Telugu
    'bn': 'bn-IN-TanishaaNeural',     # Bengali
    'kn': 'kn-IN-SapnaNeural',        # Kannada
    'gu': 'gu-IN-DhwaniNeural',       # Gujarati
    'pa': 'pa-IN-OjaswanthNeural',    # Punjabi (if available, else hi)
}

def get_azure_voice(language: str) -> str:
    """Get the correct Azure Neural TTS voice for a language."""
    return AZURE_VOICE_MAP.get(language, AZURE_VOICE_MAP['en'])


# ── Browser TTS language code map ─────────────────────────────────────────
BROWSER_LANG_MAP = {
    'en': 'en-IN', 'hi': 'hi-IN', 'ml': 'ml-IN',
    'ta': 'ta-IN', 'te': 'te-IN', 'bn': 'bn-BD',
    'kn': 'kn-IN', 'gu': 'gu-IN', 'pa': 'pa-IN',
}

def get_browser_lang(language: str) -> str:
    """Get BCP-47 language code for browser SpeechSynthesis."""
    return BROWSER_LANG_MAP.get(language, 'en-IN')
