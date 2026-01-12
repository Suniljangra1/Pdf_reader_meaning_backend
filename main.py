from fastapi import FastAPI, UploadFile, File
from fastapi.responses import FileResponse
import pdfplumber
import fitz  # PyMuPDF
import requests
import re
import shutil
import logging
from fastapi.middleware.cors import CORSMiddleware

# ---------------- SETUP ----------------

app = FastAPI()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INPUT_PDF = "input.pdf"
OUTPUT_PDF = "annotated.pdf"

MAX_PAGES = 500
MAX_WORDS_PER_PAGE = 20
MIN_WORD_LENGTH = 7
LOW_FREQUENCY_THRESHOLD = 500
MAX_SYLLABLES = 3
meaning_cache = {}

# ---------------- HELPERS ----------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:PORT", "http://localhost:9000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def count_syllables(word):
    word = word.lower()
    vowels = "aeiouy"
    syllable_count = 0
    previous_was_vowel = False
    
    for char in word:
        is_vowel = char in vowels
        if is_vowel and not previous_was_vowel:
            syllable_count += 1
        previous_was_vowel = is_vowel
    
    # Adjust for silent 'e' at the end
    if word.endswith('e') and syllable_count > 1:
        syllable_count -= 1

    logger.debug(f"Syllables | word='{word}' | count={syllable_count}")
    # Every word has at least one syllable
    return max(1, syllable_count)


def has_complex_patterns(word):
    # Consonant clusters (3+ consonants together)
    consonants = "bcdfghjklmnpqrstvwxyz"
    cluster_count = 0
    
    for i in range(len(word) - 2):
        if word[i] in consonants and word[i+1] in consonants and word[i+2] in consonants:
            return True
        

    logger.debug(f"Complex pattern | word='{word}' | consonant cluster found")

    complex_patterns = [
        # Silent letters
        'gh', 'kn', 'wr', 'ps', 'mn', 'gn', 'pn', 'mb', 'bt',
        # Digraphs & blends
        'ph', 'ch', 'sh', 'th', 'wh', 'ck', 'tch', 'dge',
        # Vowel teams (irregular sounds)
        'ea', 'ee', 'ie', 'ei', 'ai', 'ay',
        'oa', 'oe', 'oi', 'oy','ou', 'ow',
        'au', 'aw',
        'ew', 'ue', 'ui',
        # R-controlled vowels
        'ar', 'er', 'ir', 'or', 'ur',
        # Tricky endings
        'tion', 'sion', 'cian', 'ture',
        'sure', 'age', 'ous', 'ious', 'eous',
        # Greek / Latin patterns
        'psy', 'chr', 'rh', 'the', 'phon',
        'graph', 'meter', 'scope',
        # Double consonants
        'bb', 'cc', 'dd', 'ff', 'gg',
        'll', 'mm', 'nn', 'pp', 'rr', 'ss', 'tt', 'zz',
        # Rare / irregular
        'que', 'gue', 'eigh', 'augh', 'ough'
    ]

    for pattern in complex_patterns:
        if pattern in word:
            logger.debug(f"Complex pattern | word='{word}' | pattern='{pattern}'")
            return True

    return False


def is_common_word(word):
    # Sample of common English words - in real app, use a larger database
    common_words = {
        'the', 'be', 'to', 'of', 'and', 'a', 'in', 'that', 'have', 'i',
        'it', 'for', 'not', 'on', 'with', 'he', 'as', 'you', 'do', 'at',
        'this', 'but', 'his', 'by', 'from', 'they', 'we', 'say', 'her', 'she',
        'or', 'an', 'will', 'my', 'one', 'all', 'would', 'there', 'their', 'what',
        'so', 'up', 'out', 'if', 'about', 'who', 'get', 'which', 'go', 'me',
        'when', 'make', 'can', 'like', 'time', 'no', 'just', 'him', 'know', 'take',
        'people', 'into', 'year', 'your', 'good', 'some', 'could', 'them', 'see', 'other',
        'than', 'then', 'now', 'look', 'only', 'come', 'its', 'over', 'think', 'also',
        'back', 'after', 'use', 'two', 'how', 'our', 'work', 'first', 'well', 'way',
        'even', 'new', 'want', 'because', 'any', 'these', 'give', 'day', 'most', 'us',
        'cat', 'dog', 'house', 'water', 'food', 'run', 'walk', 'talk', 'big', 'small',
        'happy', 'sad', 'yes', 'no', 'hello', 'world', 'love', 'help', 'home', 'school'
    }
    
    
    return word in common_words


def is_word_hard(word: str):
    original = word
    word = word.lower().strip()

    score = 0
    reasons = []

    logger.debug(f"Checking word='{original}'")

    # 1️⃣ Length
    if len(word) > 8:
        score += 1
        reasons.append("long_word")
        logger.debug("  +1 long_word")

    # 2️⃣ Syllables
    syllables = count_syllables(word)
    if syllables > 3:
        score += 1
        reasons.append("many_syllables")
        logger.debug(f"  +1 many_syllables ({syllables})")

    # 3️⃣ Complex patterns
    if has_complex_patterns(word):
        score += 1
        reasons.append("complex_pattern")
        logger.debug("  +1 complex_pattern")

    # 4️⃣ Common word
    if not is_common_word(word):
        score += 1
        reasons.append("uncommon_word")
        logger.debug("  +1 uncommon_word")

    is_hard = score >= 2

    logger.info(
        f"Result | word='{original}' | score={score} | hard={is_hard} | reasons={reasons}"
    )

    return is_hard, reasons




def get_meaning(word: str):
    word = word.lower()

    if word in meaning_cache:
        return meaning_cache[word]

    try:
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
        r = requests.get(url, timeout=3)

        if r.status_code != 200:
            return None

        data = r.json()[0]
        sections = []
        example_count = 0
        meaning_count = 0

        for meaning in data.get("meanings", []):
            for definition in meaning.get("definitions", []):
                if meaning_count < 2:
                    sections.append(f"• {definition['definition']}")
                    meaning_count += 1

                if "example" in definition and example_count < 3:
                    sections.append(f"  Example: {definition['example']}")
                    example_count += 1

                if meaning_count >= 2 and example_count >= 3:
                    break

        final_text = f"{word.capitalize()}\n" + "\n".join(sections)

        meaning_cache[word] = final_text
        return final_text

    except Exception:
        return None


# ---------------- API ----------------

@app.post("/upload-pdf/")
# async def upload_pdf(file: UploadFile = File(...)):
async def upload_pdf(file: UploadFile = File(...)):
    # Save uploaded PDF
    with open(INPUT_PDF, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    logger.info("PDF uploaded successfully")

    text_pdf = pdfplumber.open(INPUT_PDF)
    doc = fitz.open(INPUT_PDF)

    total_pages = min(len(text_pdf.pages), MAX_PAGES)
    logger.info(f"Total pages to process: {total_pages}")

    for page_number in range(total_pages):
        logger.info(f"Processing page {page_number + 1}/{total_pages}")

        page_text = text_pdf.pages[page_number].extract_text() or ""
        words = list(set(re.findall(r"[A-Za-z]+", page_text)))

        hard_words = []

        # for w in words:
        #     is_hard, reasons = is_word_hard(w)
        #     logger.debug(f"Word='{w}' | hard={is_hard} | reasons={reasons}")
        #     if is_hard:
        #         hard_words.append((w, reasons))

        for w in words:
            #  Fast skip for short words (performance)
            if len(w) < MIN_WORD_LENGTH:
                logger.debug(f"Skipping '{w}' | length={len(w)} < MIN_WORD_LENGTH={MIN_WORD_LENGTH}")
                continue

            is_hard, reasons = is_word_hard(w)
            logger.debug(f"Word='{w}' | hard={is_hard} | reasons={reasons}")
            if is_hard:
                hard_words.append((w, reasons))

        hard_words = hard_words[:MAX_WORDS_PER_PAGE]
        logger.info(f"Page {page_number + 1}: Found {len(hard_words)} hard words")
        pdf_page = doc[page_number]
        for word, reasons in hard_words:
            logger.info(
                f"Checking word '{word}' | Reasons: {', '.join(reasons)}"
            )

            meaning = get_meaning(word)

            if not meaning:
                logger.warning(f"No meaning found for '{word}'")
                continue

            locations = pdf_page.search_for(word)
            if not locations:
                logger.warning(
                    f"'{word}' found in text but not visually on page"
                )
                continue

            rect = locations[0]

            # UNDERLINE (clean, readable)
            underline = pdf_page.add_underline_annot(rect)
            underline.set_colors(stroke=(0, 0, 0))
            underline.update()

            # HIGHLIGHT (primary interaction)
            highlight = pdf_page.add_highlight_annot(rect)
            highlight.set_colors(stroke=(1, 0.95, 0.6))
            highlight.set_opacity(1.0)

            highlight.set_info({
                "title": "Meaning",
                "content": meaning
            })

            highlight.update()

            logger.info(
                f"Annotated '{word}' on page {page_number + 1}"
            )

    doc.save(OUTPUT_PDF)
    doc.close()
    text_pdf.close()

    logger.info("PDF processing complete successfully")

    return FileResponse(
        OUTPUT_PDF,
        media_type="application/pdf",
        filename="annotated_book.pdf"
    )




# Test examples
test_words = [
    "cat",           # Easy: short, common, simple
    "dog",           # Easy: short, common, simple
    "beautiful",     # Medium: longer but common
    "mitochondria",  # Hard: long, many syllables, uncommon
    "algorithm",     # Hard: long, technical
    "psychology",    # Hard: silent 'p', longer
    "knight",        # Medium-Hard: silent 'k'
    "run",           # Easy: short, common
    "chrysanthemum", # Hard: long, many syllables, complex
    "the"            # Easy: very common
]
