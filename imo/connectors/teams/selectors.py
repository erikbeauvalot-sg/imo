"""Sélecteurs Playwright de l'interface Teams web (invité anonyme).

Le DOM de Teams change d'une version à l'autre : tous les sélecteurs sont
regroupés ici pour être ajustés sans toucher à la logique. Le navigateur est
lancé en locale en-US, d'où des libellés anglais.

Adapté de Vexa (https://github.com/Vexa-ai/vexa, Apache-2.0),
core/meetings/modules/join/src/msteams/selectors.ts — voir NOTICE.
"""

# Page d'accueil « Comment voulez-vous rejoindre ? »
CONTINUE_IN_BROWSER = [
    'button:has-text("Continue on this browser")',
    'button:has-text("Join on the web instead")',
    'a:has-text("Join on the web instead")',
    'button:has-text("Continue")',
]

# Fenêtre « Are you sure you don't want audio or video? »
CONTINUE_WITHOUT_MEDIA = [
    'button:has-text("Continue without audio or video")',
    'button[aria-label*="Continue without audio"]',
]

# Écran de pré-connexion
NAME_INPUT = [
    'input[data-tid="prejoin-display-name-input"]',
    'input[placeholder*="name"]',
    'input[placeholder*="Name"]',
    'input[type="text"]',
]
JOIN_NOW = [
    'button[data-tid="prejoin-join-button"]',
    'button:has-text("Join now")',
]
# Interrupteurs caméra / micro : le libellé décrit l'action, donc « Turn camera off »
# signifie que la caméra est allumée. On ne clique que dans ce cas (bascule).
CAMERA_ON = [
    '[role="switch"][data-tid="toggle-video"][title^="Turn camera off"]',
    '[role="switch"][title^="Turn camera off"]',
    'button[aria-label^="Turn camera off"]',
    'button[aria-label^="Turn off camera"]',
]
CAMERA_OFF = [
    '[role="switch"][data-tid="toggle-video"][title^="Turn camera on"]',
    '[role="switch"][title^="Turn camera on"]',
    'button[aria-label^="Turn camera on"]',
    'button[aria-label^="Turn on camera"]',
]
MIC_ON = [
    '[role="switch"][data-tid="toggle-mute"][title^="Mute mic"]',
    '[role="switch"][title^="Mute mic"]',
    'button[aria-label^="Mute mic"]',
]
DONT_USE_AUDIO = [
    'input[type="radio"][aria-label="Don\'t use audio"]',
    '[role="radio"][aria-label*="Don\'t use audio"]',
    '[role="radio"]:has-text("Don\'t use audio")',
]

# Salle d'attente
LOBBY = [
    "text=Someone will let you in shortly",
    "text=You're in the lobby",
    "text=Waiting for someone to let you in",
    "text=Your request to join has been sent",
]

# Admis dans la réunion : le bouton « Quitter » est présent
IN_MEETING = [
    'button[id="hangup-button"]',
    'button[data-tid="hangup-main-btn"]',
    'button[aria-label="Leave"]',
    '[role="toolbar"] button[aria-label*="Leave"]',
]
HANGUP = IN_MEETING

# Refus d'admission
REJECTED = [
    "text=Sorry, but you were denied",
    "text=You were denied entry",
    "text=Access denied",
    "text=Unable to join",
]

# Retiré de la réunion / réunion terminée (texte uniquement : les alertes
# génériques de Teams déclenchent de faux positifs)
REMOVED_OR_ENDED = [
    "text=You've been removed from this meeting",
    "text=You’ve been removed from this meeting",
    "text=You have been removed from this meeting",
    "text=Meeting ended",
    "text=Call ended",
    "text=The meeting has ended",
]

# Bouton d'ouverture du panneau de chat
CHAT_BUTTON = [
    'button[id="chat-button"]',
    'button[data-tid="chat-button"]',
    'button[aria-label="Chat"]',
    'button[aria-label*="Show conversation"]',
    'button[aria-label*="Chat"]',
]

# Zone de saisie du chat (absente si le chat est fermé aux invités)
CHAT_INPUT = [
    '[data-tid="ckeditor"][contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"][aria-label*="Type a message"]',
    'div[contenteditable="true"][aria-label*="message"]',
    'div[role="textbox"][contenteditable="true"]',
]

# Sous-titres en direct : menu « More » → « Language and speech » → « Turn on live captions ».
# Sélecteurs à confirmer sur le vrai Teams (captures « captions-* » du dossier debug).
MORE_BUTTON = [
    'button[id="callingButtons-showMoreBtn"]',
    'button[data-tid="more-button"]',
    'button[aria-label="More"]',
    'button[aria-label="More actions"]',
    '[role="toolbar"] button[aria-label*="More"]',
]
LANGUAGE_SPEECH_MENU = [
    '[data-tid="LanguageSpeechMenuControl-id"]',
    '[role="menuitem"]:has-text("Language and speech")',
    'button:has-text("Language and speech")',
    '[role="menuitem"]:has-text("Language")',
]
CAPTIONS_ON = [
    '[data-tid="closed-captions-button"]',
    '[role="menuitem"]:has-text("Turn on live captions")',
    '[role="menuitemcheckbox"]:has-text("Live captions")',
    '[role="menuitem"]:has-text("Show live captions")',
    'button:has-text("Turn on live captions")',
    '[role="menuitem"]:has-text("Captions")',
    '[role="menuitem"]:has-text("captions")',
    '[role="menuitemcheckbox"]:has-text("aptions")',
]
# Sous-menu éventuel après « Captions »
CAPTIONS_SUBMENU = [
    '[role="menuitem"]:has-text("Turn on live captions")',
    '[role="menuitemcheckbox"]:has-text("Live captions")',
    '[role="menuitem"]:has-text("Show live captions")',
    '[role="menuitem"]:has-text("Live captions")',
    '[role="menuitem"]:has-text("Turn on")',
]
# Zone des sous-titres, présente une fois activés (la lecture est dans captions_reader.js)
CAPTIONS_CONTAINER = [
    '[data-tid="closed-captions-renderer"]',
    '[data-tid="closed-caption-v2-window-wrapper"]',
    '[data-tid*="closed-caption"]',
]

# Langue parlée des sous-titres : « … » du bandeau des sous-titres (ou menu More → Language and
# speech) → « Language settings » → liste « Spoken language » → langue → « Update ».
# Sélecteurs à confirmer sur le vrai Teams (captures « language-* » du dossier debug).
SPOKEN_LANGUAGES = {"fr": "French (France)", "en": "English (United States)"}
CAPTIONS_SETTINGS_BUTTON = [
    '[data-tid="closed-captions-settings-button"]',
    '[data-tid*="closed-caption"] button[aria-label*="settings"]',
    '[data-tid*="closed-caption"] button[aria-label*="options"]',
    'button[aria-label*="Captions settings"]',
    'button[aria-label*="Caption settings"]',
]
LANGUAGE_SETTINGS = [
    '[role="menuitem"]:has-text("Language settings")',
    '[role="menuitem"]:has-text("Change spoken language")',
    '[role="menuitem"]:has-text("Spoken language")',
    'button:has-text("Language settings")',
]
SPOKEN_LANGUAGE_DROPDOWN = [
    '[data-tid="spoken-language-dropdown"]',
    '[role="combobox"][aria-label*="poken language"]',
    'button[aria-label*="poken language"]',
    '[role="combobox"]',
]
SPOKEN_LANGUAGE_CONFIRM = [
    'button:has-text("Update")',
    'button:has-text("Confirm")',
    'button:has-text("Save")',
    'button:has-text("Apply")',
]


# Messages d'événement affichés par Teams dans le chat (expressions régulières,
# insensibles à la casse) : enregistrés avec source="system", jamais pris pour des commandes.
SYSTEM_MESSAGES = [
    r"\bhas (temporarily )?joined the (chat|meeting|conversation)\.?$",
    r"\b(has )?left the (chat|meeting|conversation)\.?$",
    r"^(the )?meeting (started|ended)\b",
    r"\bstarted (recording|transcription|transcribing)\b",
    r"\bstopped (recording|transcription|transcribing)\b",
]

# Hôtes de connexion Microsoft : la réunion exige un compte (invités refusés)
LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com")
