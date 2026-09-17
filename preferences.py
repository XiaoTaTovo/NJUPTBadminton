"""User-defined Xianlin preference; not a statement of official court classifications."""
import re
import unicodedata

LABELS = (
    '优先1：仙林体育馆3楼双打（1–8号）',
    '优先2：仙林体育馆3楼单打（9–12号）',
    '优先3：仙林体育馆1楼（不区分单双打）',
    '优先4：仙林训练馆2楼（不区分单双打）',
    '其他或无法识别（按原候选顺序）',
)
RULE = '体育馆3楼双打 > 体育馆3楼单打 > 体育馆1楼任意 > 训练馆2楼任意'


def integer(text):
    if text.isdecimal():
        return int(text)
    digits={'零':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if text in digits:
        return digits[text]
    if text.count('十')==1:
        left,right=text.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return (digits[left] if left else 1)*10+(digits[right] if right else 0)
    return None


def normalize(name):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',name)).upper()


def floor_number(name):
    match=re.search(r'([0-9一二两三四五六七八九十]+)(?:F|楼|层)',normalize(name))
    return integer(match[1]) if match else None


def court_number(name):
    # Anchor to the court suffix so floor digits cannot be interpreted as court numbers.
    match=re.search(r'([0-9零一二两三四五六七八九十]+)(?:号)?(?:场地|场)$',normalize(name))
    return integer(match[1]) if match else None


def tier(name):
    text=normalize(name)
    floor=floor_number(name)
    if '仙林体育馆' in text:
        if floor==3:
            court=court_number(name)
            if court is not None and 1<=court<=8: return 0
            if court is not None and 9<=court<=12: return 1
        if floor==1: return 2
    if '仙林训练馆' in text and floor==2: return 3
    return 4


def label(name):
    return LABELS[tier(name)]


def ordered(names):
    """Stable sort: do not impose doubles priority on the 1F/2F groups."""
    return sorted(names,key=tier)
