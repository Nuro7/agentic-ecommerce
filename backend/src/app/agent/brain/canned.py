"""Static response constants and multi-language reply helpers."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

_SUPPORTED_LANGS = {"en", "hi", "ml", "ta", "te", "bn", "kn", "gu", "pa"}

_CHITCHAT_RESPONSES: Dict[str, List[str]] = {
    "en": [
        "Hey! I'm here to help you shop. What are you looking for?",
        "Hi there! Ready to help you find something great. What do you need?",
        "Hello! Just ask me about any product or your cart and I'll help out.",
    ],
    "hi": [
        "Namaste! Kya dhundhna hai aapko? Main help kar sakta hoon.",
        "Hi! Koi product chahiye? Ya cart dekhna hai? Batao!",
        "Hello! Main yahan hoon — kuch bhi poochho.",
    ],
    "ml": [
        "Namaskaram! Enthu venam? Njaan sahaayikkam.",
        "Hi! Enthu product thedunnu? Paranjaal mathi.",
        "Hello! Cart nokkano, enthelum thedano? Parayo.",
    ],
    "ta": [
        "Vanakkam! Enna thedugirirkal? Naan udavukirom.",
        "Hi! Enna venum? Kelu, naan solli taruvom.",
        "Hello! Cart paarkkavenuma, illai products thedavenuma?",
    ],
    "te": [
        "Namaskaram! Emi kavali? Nenu help chestanu.",
        "Hi! Products chudaniki or cart chudaniki — emi kavali?",
    ],
    "bn": [
        "Namaskar! Kī khujchen? Āmi sahāẏa korbo.",
        "Hi! Kono product lagbe? Bolun, ami help korbo.",
    ],
    "kn": [
        "Namaskara! Yenu beku? Nānu sahāya māḍuttēne.",
        "Hi! Products nōḍabēkā? Kēḷi, nānu help māḍuttēne.",
    ],
}

_OFF_TOPIC_RESPONSES: Dict[str, str] = {
    "en": "I'm here just for shopping at this store. What product can I help you find?",
    "hi": "Main sirf is store ki shopping mein help karta hoon. Kya dhundhna hai?",
    "ml": "Njan ith store-ile shopping mathrame sahayikkoo. Enthu venam?",
    "ta": "Naan ith kadaiyil shopping mathrame help seigiren. Enna thedugirirkal?",
    "te": "Nenu ee store shopping matrame help chestanu. Emi kavali?",
    "bn": "Āmi shudhu ei store-er shopping-e help kori. Kī lagbe?",
    "kn": "Nānu ee store shopping mātra help māḍuttēne. Yenu beku?",
}


def normalize_language(language: Optional[str]) -> str:
    raw = str(language or "en").strip().lower()
    if raw in _SUPPORTED_LANGS:
        return raw
    for lang in _SUPPORTED_LANGS:
        if raw.startswith(lang):
            return lang
    return "en"


def chitchat_response(lang: str, session_id: str) -> Dict[str, Any]:
    from .text_utils import with_actions_alias
    options = _CHITCHAT_RESPONSES.get(lang) or _CHITCHAT_RESPONSES["en"]
    idx = int(hashlib.md5(session_id.encode()).hexdigest(), 16) % len(options)
    text = options[idx]
    return with_actions_alias({
        "response_text": text,
        "ui_actions": [],
        "suggested_replies": ["Show products", "Show my cart", "Store info"],
    })


def off_topic_response(lang: str) -> Dict[str, Any]:
    from .text_utils import with_actions_alias
    text = _OFF_TOPIC_RESPONSES.get(lang) or _OFF_TOPIC_RESPONSES["en"]
    return with_actions_alias({
        "response_text": text,
        "ui_actions": [],
        "suggested_replies": ["Show products", "Show my cart", "Store info"],
    })


def say(language: str, key: str, **kwargs: Any) -> str:
    templates: Dict[str, Dict[str, str]] = {
        "en": {
            "checkout_triggered": "Sure. Let me collect your delivery details first.",
            "cart_opened": "Here is your current cart.",
            "cart_empty": "Your cart is empty right now.",
            "removed_from_cart": "Removed {name} from your cart.",
            "no_products": "I couldn't find a close match. Try product name, brand, size, or budget.",
            "no_products_specific": "I couldn't find anything matching '{query}' in this store. Try a different search or browse our catalog.",
            "products_found": "{name} looks like your best bet. Want me to show the size and color options?",
            "ask_product_for_stock": "Tell me the product name and size, and I'll check live stock.",
            "availability": "{name} {size_text}is {stock_text}{qty_text}.",
            "need_two_compare": "Please name at least two products to compare.",
            "comparison_ready": "I compared the options for you. Do you want me to add one to cart?",
            "store_info": "This store is {store_name}. I can help you find products, compare options, and checkout faster.",
            "ask_order_email": "Please share your order email and I'll fetch your latest status.",
            "order_not_found": "I couldn't find recent orders for that email.",
            "order_status": "Your latest order #{order_no} is {status}.",
            "ask_add_which": "Tell me which product to add, or say add the first one.",
            "out_of_stock": "{name} {size_text}is out of stock. I can show similar options.",
            "added_to_cart": "Done! {name} (qty: {qty}) is in your cart. Would you like to add anything else or proceed to checkout?",
            "ask_variation": "{name} comes in these options: {options}. Which one would you like, and how many?",
        },
        "hi": {
            "checkout_triggered": "Bilkul. Chaliye delivery details lete hain.",
            "cart_opened": "Yeh aapka current cart hai.",
            "cart_empty": "Aapka cart abhi khaali hai.",
            "removed_from_cart": "Maine {name} cart se hata diya.",
            "no_products": "Exact match nahi mila. Product name, brand ya budget boliye.",
            "products_found": "{name} best option lagta hai. Size aur color options dikhaaun?",
            "ask_product_for_stock": "Product ka naam aur size batayein, main live stock check karta hoon.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare ke liye kam se kam do products boliye.",
            "comparison_ready": "Maine options compare kar diye. Kya ek cart mein add kar doon?",
            "store_info": "Is store ka naam {store_name} hai. Main products dhoondne, compare karne, aur checkout mein help kar sakta hoon.",
            "ask_order_email": "Order status ke liye apna email batayein.",
            "order_not_found": "Is email ke liye recent order nahi mila.",
            "order_status": "Aapka latest order #{order_no} abhi {status} hai.",
            "ask_add_which": "Kaunsa product add karna hai? Ya bolo first wala add karo.",
            "out_of_stock": "{name} {size_text}stock mein nahi hai. Similar options dikhaun?",
            "added_to_cart": "Done! {name} ({qty} nos.) cart mein add ho gaya. Kuch aur add karein ya checkout karein?",
            "ask_variation": "{name} yeh options mein available hai: {options}. Kaunsa chahiye aur kitne?",
        },
        "ml": {
            "checkout_triggered": "ശരി. Delivery details edukkam.",
            "cart_opened": "Ithaa ningalude cart.",
            "cart_empty": "Cart ippol khaaliyannu.",
            "removed_from_cart": "{name} cart-il ninnum neekkiyirikkunnu.",
            "no_products": "Onnum kittiyilla. Product peru, brand, athava budget parayoo.",
            "products_found": "{name} best option aanu. Size, color options kaanano?",
            "ask_product_for_stock": "Product peru, size parayoo — stock check cheyyam.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare cheyyaan rantu products parayoo.",
            "comparison_ready": "Njaan options compare cheythu. Onnu cart-il ittekkaano?",
            "store_info": "{store_name} aanu ee store. Products kandittu, compare cheythu, checkout cheyyaan help cheyyam.",
            "ask_order_email": "Order status ariyaan email parayoo.",
            "order_not_found": "Aa email-il order kittiyilla.",
            "order_status": "Ningalude latest order #{order_no} ippol {status} aanu.",
            "ask_add_which": "Ethu product add cheyyano? First onnu parayoo.",
            "out_of_stock": "{name} {size_text}stock illaa. Similar options kaanano?",
            "added_to_cart": "Done! {name} ({qty} nos.) cart-il undi. Ingane mattonninum veno, checkout cheyyano?",
            "ask_variation": "{name} ithaa options: {options}. Ethu veno, etthu veno?",
        },
        "ta": {
            "checkout_triggered": "Sari. Delivery details vaangurom.",
            "cart_opened": "Ingae ungal cart irukku.",
            "cart_empty": "Cart ippo kaaliyannu.",
            "removed_from_cart": "{name} cart-la irundhu eduthutten.",
            "no_products": "Onnum kanavillai. Product peyar, brand, athava budget sollunga.",
            "products_found": "{name} best choice. Size, color options paakalamaa?",
            "ask_product_for_stock": "Product peyar, size sollunga — stock check pannuven.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare panna rendu products sollunga.",
            "comparison_ready": "Nangu compare panniten. Onnu cart-la podalaamaa?",
            "store_info": "Ithu {store_name}. Products thedu, compare pannu, checkout aaga help pannuven.",
            "ask_order_email": "Order status therinja email sollunga.",
            "order_not_found": "Aa email-la order kanavillai.",
            "order_status": "Ungal recent order #{order_no} ippo {status} la irukku.",
            "ask_add_which": "Etha product add pannanum? First onnu sollunga.",
            "out_of_stock": "{name} {size_text}stock-la illai. Similar options paakalamaa?",
            "added_to_cart": "Aayittu! {name} ({qty} nos.) cart-la irukku. Vera enna venom, checkout panalaamaa?",
            "ask_variation": "{name}-ku ithu options: {options}. Etha venom, evvalavu venom?",
        },
        "te": {
            "checkout_triggered": "Sare. Delivery details teesukuntam.",
            "cart_opened": "Meeru cart ivigo.",
            "cart_empty": "Cart ipudu khaaliganundi.",
            "removed_from_cart": "{name} cart nundi teesaanu.",
            "no_products": "Emi dorkaledu. Product peru, brand, budget cheppandi.",
            "products_found": "{name} best option. Size, color options chupimma?",
            "ask_product_for_stock": "Product peru, size cheppandi — stock check chestanu.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare cheyyataniki rendu products cheppandi.",
            "comparison_ready": "Nannu options compare chesanu. Okati cart lo veyyanaa?",
            "store_info": "Idi {store_name}. Products vethakataniki, compare cheyyataniki, checkout ki help chestanu.",
            "ask_order_email": "Order status kosam email cheppandi.",
            "order_not_found": "Aa email ki order dorkaledu.",
            "order_status": "Meeru recent order #{order_no} ipudu {status} lo undi.",
            "ask_add_which": "Etha product add cheyyali? Modati okati cheppandi.",
            "out_of_stock": "{name} {size_text}stock lo ledu. Similar options chupimma?",
            "added_to_cart": "Aipoyindi! {name} ({qty} nos.) cart lo undi. Inkemi veladam, checkout chesdam?",
            "ask_variation": "{name} ki ee options unnai: {options}. Edu kavali, enni kavali?",
        },
        "bn": {
            "checkout_triggered": "Thik ache. Delivery details neoa jak.",
            "cart_opened": "Ei je aapnar cart.",
            "cart_empty": "Aapnar cart ekhon khali.",
            "removed_from_cart": "{name} cart theke sore diyechi.",
            "no_products": "Kono mael paoaa jaini. Product naam, brand ba budget bolun.",
            "products_found": "{name} best option. Size, color options dekhabo?",
            "ask_product_for_stock": "Product naam, size bolun — stock check korbo.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare korar jonno duto product bolun.",
            "comparison_ready": "Ami options compare korechi. Ekta cart e debo?",
            "store_info": "Ei store er naam {store_name}. Products khuje, compare kore, checkout e help korbo.",
            "ask_order_email": "Order status er jonno email bolun.",
            "order_not_found": "Oi email e order paoaa jaini.",
            "order_status": "Aapnar recent order #{order_no} ekhon {status} e ache.",
            "ask_add_which": "Kon product add korbo? Prothomta bolun.",
            "out_of_stock": "{name} {size_text}stock e nei. Similar option dekhabo?",
            "added_to_cart": "Hoyeche! {name} ({qty} nos.) cart e ache. Ar kichu lagbe, checkout korben?",
            "ask_variation": "{name} r ei options ache: {options}. Konta lagbe, koto lagbe?",
        },
        "kn": {
            "checkout_triggered": "Sari. Delivery details tegedukoLona.",
            "cart_opened": "Nimage cart illi ide.",
            "cart_empty": "Cart ippudu khaali agi ide.",
            "removed_from_cart": "{name} cart ninda bitti.",
            "no_products": "Yenu sikkililla. Product hesaru, brand, athava budget heli.",
            "products_found": "{name} best choice. Size, color options nodona?",
            "ask_product_for_stock": "Product hesaru, size heli — stock check madutta.",
            "availability": "{name} {size_text}{stock_text}{qty_text}.",
            "need_two_compare": "Compare maadalu eradu products heli.",
            "comparison_ready": "Naanu options compare madide. Ondu cart ge hako?",
            "store_info": "Ee store hesaru {store_name}. Products houdi, compare maadi, checkout ge help madutta.",
            "ask_order_email": "Order status ge email heli.",
            "order_not_found": "Aa email ge order sikkililla.",
            "order_status": "Nimage recent order #{order_no} ippudu {status} alli ide.",
            "ask_add_which": "Yaavudu product add maadali? Modalu ondu heli.",
            "out_of_stock": "{name} {size_text}stock alli illa. Similar options nodona?",
            "added_to_cart": "Aayitu! {name} ({qty} nos.) cart alli ide. Innu bere beku, checkout maadona?",
            "ask_variation": "{name} ge ee options idhe: {options}. Yaavudu beku, eshtu beku?",
        },
    }
    table = templates.get(language, templates["en"])
    tpl = table.get(key, templates["en"].get(key, ""))

    size = str(kwargs.get("size") or "").strip()
    qty = kwargs.get("qty")
    in_stock_val = kwargs.get("in_stock")
    kwargs["size_text"] = (f"size {size} " if size else "")
    if in_stock_val is None:
        kwargs["stock_text"] = ""
        kwargs["qty_text"] = ""
    else:
        kwargs["stock_text"] = "is available" if in_stock_val else "is currently unavailable"
        kwargs["qty_text"] = f" with only {qty} left" if isinstance(qty, int) else ""

    try:
        return tpl.format(**kwargs)
    except KeyError:
        return tpl
