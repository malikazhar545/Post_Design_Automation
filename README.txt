POST DESIGN AUTOMATION — kaise chalana hai
==========================================

PEHLI DAFA (sirf ek baar)
-------------------------
1) 1_SETUP_install.bat  par double-click.

2) Har Chrome profile mein us ki extension "Load unpacked" karo
   (folder: D:\Post Design Automation\extensions\...):

   Profile                              Folder
   ----------------------------------   ---------------------
   Designer mail (Docs + Freepik)       Extension_DESIGNER
   Waseem (ChatGPT designs)             Extension_WASEEM
   Wajid (ChatGPT instructions)         Extension_WAJID
   Azhar Testing (Instagram)            Extension_INSTAGRAM
   Envato wali profile                  Extension_ENVATO

   chrome://extensions -> Developer mode ON -> Load unpacked -> folder.
   Har profile mein: chrome://settings/performance -> Memory Saver OFF.
   (Code update ho to extensions KHUD reload ho jati hain — dobara kuch nahi karna.)

ROZ
---
1) START_SERVER.bat chalao (kali window — band NA karna).
2) Paanchon Chrome profiles khule rahen.
3) Designer profile mein aaj ki Google Docs files kholo (30 bhi ho sakti hain).
4) Designer extension ka icon -> sab "connected" -> START.
   Script EK WAQT MEIN EK file karti hai: images ban kar, check ho kar, download ho kar hi agli file.

IMAGES KAHAN
------------
Downloads\<date>\<company>\   (posts, stories, mockups — sirf final images)
Baqi sab (source images, doc ka text, ChatGPT ko gaya message, ghalat images):
   D:\Post Design Automation\Output\<date>\<file>\
Aakhir ki summary (kitni reels skip, kaun si post 'done', kya ghalat bana):
   Output\<date>\SUMMARY_<time>.txt  + popup

POPUP (Windows notification) aata hai jab:
- kisi file mein error ho (script agli file par chali jati hai)
- ChatGPT load na ho / limit lage (script khud ruk kar dobara karti hai)
- koi image 2 dafa ghalat bane (aap khud bana lena)
- saara kaam khatam ho

SCRIPT KYA KARTI HAI (har file)
-------------------------------
- File ka format kuch bhi ho: labels (INSTA/FB POST, Post 28, STORY), dates (10/3 Done), ya sirf image links.
  Na samjhe to ChatGPT (Wajid temporary chat) se parhwati hai — sirf doc ka hoobahoo text use hota hai.
- REEL skip (label se ya video link se: Instagram reel, Freepik video...) -> summary mein.
- "done" / "do not publish" wali post skip -> summary mein.
- Heading = design ka text. Heading na ho to sirf caption ki PEHLI line. Caption kabhi nahi.
- Image link kahin bhi ho (shuru / heading ke baad / caption ke baad) — usi post ki image.
  Pinterest, Freepik, Envato, Google Images, Google Drive, koi bhi image page.
  File mein image di hai to WOHI use honi zaroori — download na ho to post skip + summary.
- Instagram post link -> Azhar profile se har slide ka screenshot (caption nahi, aakhri CTA slide skip).
- Product link -> us product ki photo ke saath heading wali post. Sirf config.mockup_companies (Wear Wood) ke liye 2 alag mockups (placed + worn/use), 4:5.
- Molen jaisi files (image prompt / brief) -> brief hi content; link ho to page parh kar idea.
- Waseem mein company ki chat (poore naam + hisson se dhoond kar, content se verify) ->
  na mile to Wajid se instructions -> Waseem nayi chat (naam = company).
- Posts 4:5 (LinkedIn bhi), story 9:16, exact branding, no graphics/blobs, no extra content,
  logo ho to logo warna company naam placeholder.
- QA (1 minute): text sahi / extra text to nahi. Ghalat -> ek dafa dobara banwati hai -> phir bhi ghalat -> popup.

NOTE
----
- Jo file "done" ho chuki dobara nahi banegi (Output\state.json se entry hata do to dobara banegi).
- Sab messages / rules config.json mein badal sakte ho.
- Test: python trigger_start.py "Belle"   (sirf woh file)


UPDATED WORKFLOW (2026-10-16)
-----------------------------
- File order: Designer ki sab se right wali Google Docs tab pehle, phir left ki taraf. Preparation aur generation dono isi order mein.
- START: pehle saari pending files ki WORKING copies prepare hoti hain. Original Google Docs untouched.
- Writer ka text/order unchanged: sirf Heading:/Caption: labels add, already formatted sections unchanged.
- Ambiguous heading/caption: review error; content guess ya silent skip nahi.
- Design prompt: every heading line exact; labels/metadata must NEVER appear on the image.
- Har post / story / mockup ALAG message (asli test: ek message mein kai posts -> ChatGPT ne sirf pehli post ke variants banaye). config: posts_in_one_message=false.
- Product store (Wear Wood jaise): har mockup ALAG message (placed, phir worn/use). No collage/grid/panels.
- Each generated image is downloaded immediately. QA checks exact heading and no collage; unverified QA is not a pass.
- ChatGPT ACTIVE generation ko timeout par interrupt nahi karte.
- ChatGPT ATAK jaye (10 minute se nayi image nahi): script ki ChatGPT tab refresh -> image ban chuki ho to wahi le leti hai.
  2 refresh ke baad bhi atka -> woh image summary mein pending, baqi kaam chalta rahta hai. (config: stuck_refresh_minutes, max_refreshes)
- Ek post / QA / image ka masla poori file nahi rokta: baqi posts banti hain, masla summary mein (HEADING CHECK / QA CONFIRM NAHI / POST SAMAJH NAHI AAYI).
- Actual usage/rate limit: script pause 2 minutes, one retry. Still limited -> file pending, next file.
- Normal 'generate' text is not a rate-limit notice. Old replies cannot trigger the current request's limit pause.
- Wajid: last visible exchange contains instructions -> reuse without sending; limit -> existing instructions backup.
- Agar Waseem aur Wajid dono mein name-parts search ke baad chat na mile: file ki company website Designer ke automation group mein khulegi.
- Website se actual logo, heading/body font-family, font weights aur colours save honge; logo attach karke Waseem mein company ki nayi chat banegi.
- Website link na ho, ya website/fonts/logo/company identity verify na ho -> file manual-review list mein; next file continues. Empty branding chat nahi banti.
- Website branding evidence/logo Output\<date>\<file>\website_branding mein save hote hain.
- Extension v1.7 required for website branding; built copies are in extensions/. Server restart/reconnect par version check hota hai.
- No valid instructions -> error, never create an empty company chat.
- No extra sleep between completed files.
- Downloads\<date>\<company>\: SIRF ChatGPT ki final images. Aur kuch nahi.
- Output\<date>\<file>\: source images, working copy, prompts, QA records (Downloads mein nahi).
- Final SUMMARY (completed / pending files, reels) Output\<date>\SUMMARY_<time>.txt mein + popup.
- Incomplete files are not marked DONE. Single-post tests do not overwrite whole-file completion state.
- Load Python/config changes by restarting START_SERVER.bat after the current generation is finished.
- Local verification: python -m unittest discover -s tests -v
- Har post ka layout ALAG (variety rule, sab companies). Company ki apni instructions (3D look waghera) general rules se upar.
- config.json har file se pehle dobara parhi jati hai — rules badlo to server restart ki zaroorat nahi.
- ChatGPT ke saath kaam PAGE dekh kar (jaise insaan): image bani ya nahi, download (blob), QA jawab — sab page se. API sirf aakhri backup (429 'too many requests' se bachao).
- STYLE REFERENCE: har company ke liye D:\Azhar\October\Posts\<Company>\ se aap ke 4 sab se naye designs ChatGPT ko
  dikhaye jate hain (typography, logo, 3D, richness isi level ki). Folder naam thoda alag ho to bhi milta hai
  (E liquid = Eliquid, Dental Care 4U = dental care for you). Naye achhe designs isi folder mein daalte raho.
  Ek chat mein ek dafa upload -> agli dafa dobara nahi (Output\chat_memory.json yaad rakhta hai).

IMAGE / PRODUCT FIXES (extension v2.6)
------------------------------------
- Har product ki photo usi post/mockup ke message ke saath attach hoti hai, dobara request aur QA retry mein bhi.
- posts_in_one_message=true ho tab bhi product posts alag messages mein jati hain.
- Style references alag message mein jate hain; product requests mein dusre products attach nahi hote.
- Attachment count confirm na ho, duplicate/stale attachment ho: send nahi hota; chat dobara khol kar ek retry.
- Product photo download fail ho to post pending rehti hai; invented product ke saath generation nahi hoti.
- Image detector same turn container ke andar user message ke BAAD wali gallery bhi dekhta hai.
- Purani image ka signed URL badalne se woh nayi image nahi gini jati. Unloaded previews bhi ready nahi ginte.
- Page par image na mile to current conversation branch ka API backup; duplicate request se pehle dobara verification.
- API verification fail ho to duplicate generation message nahi jata; image pending report hoti hai.
- Parser: Post#2 / emoji labels / label ke saath link / Headings: / magnific images aur videos supported.
- Carousel ki har slide alag image; text-less story mein source image ka exact text use hota hai.
- Code load karne ke liye current generation complete hone par START_SERVER.bat restart karo.
  Server reconnect par extensions v2.6 khud load kar lengi.
- Checks: python -m unittest discover -s tests -q
  node tests/image-detection-check.cjs
  node tests/brand-snapshot-check.cjs

STOP / PAUSE, INTERNET, BIJLI (resume)
--------------------------------------
- Designer extension popup: START / STOP-PAUSE. STOP -> current step khatam kar ke ruk jata hai
  ("Ruk gaya" aaye to net band kar sakte ho). Ghar aa kar START -> wahin se resume.
- Har ban chuki image foran yaad (Output\progress.json). File beech mein ruke (stop / net / bijli / restart)
  -> agli dafa sirf BAQI images banti hain, pehle wali dobara nahi.
- Internet chala jaye: script khud ruk jati hai (popup), net aate hi wahi file wahin se. File error nahi hoti.
- Bijli / PC restart: Windows login par AUTO_START.bat khud chalta hai (5 Chrome profiles + server).
  Server dekhta hai pichla run adhoora tha (Output\run_active.json) -> profiles judne par khud START,
  band files khud kholta hai. PAUSE par tha to khud shuru NAHI karta.
  Auto-start hatana: UNINSTALL_AUTO_START.bat   |   dobara lagana: INSTALL_AUTO_START.bat
- Laptop ho to bijli jaane par battery par chalta rehta hai — tab sirf internet wala hissa kaam aata hai.

UPDATE (2026-10-18)
-------------------
- BEFORE / AFTER (construction jaisi files): "Before: link" + "After: link" -> DONO images lazmi.
  config before_after_mode = "carousel" (slide 1 before photo + chhota "BEFORE" neeche left, slide 2 after + "AFTER")
  ya "comparison" (ek image mein dono photos). Ek image download na ho -> poori post skip + summary.
- "HEADLINE:" bhi heading label hai (design par print nahi hota).
- company_aliases (config): file ek naam ki, chat doosri company ki. Ficus Financial -> Beyond the Budget.
- Freepik/Magnific: download 2 dafa fail -> page ki preview image ka screenshot (post skip nahi).
- Carousel: saari slides EK message mein (10 ki had nahi). Koi slide na bane -> usi carousel ka
  "continue" message (carousel_extra_tries = 2), phir bhi na bane -> alag message mein 2 koshishen.
- Downloads\<date>\<company>\<file number>\  (Wear Wood 7 -> Wear Wood\7, Wear Wood 8 -> Wear Wood\8).
  File naam mein number na ho -> seedha company folder. Alias file (Ficus) -> Beyond the Budget\Ficus Financial 6.
- ENVATO: extension band (config disabled_roles = ["envato"]). Envato link hamesha Azhar (Instagram) profile mein
  khulta hai aur sirf preview ka screenshot liya jata hai — download nahi (config envato_screenshot_role).
- Sirf STORY reh jaye (posts sab ban gayin): file DONE, story summary mein "[STORY SKIP - aap khud banao]".
  Posts Downloads mein. Koi POST reh jaye to file pehle ki tarah incomplete.
- Instagram post batch mein hi: "Attached image N is a screenshot of an Instagram post — EXACT same content + relevant image, my branding, 4:5". Writer ki image: "Use attached image N with this content".
- Duplicate check: ChatGPT wahi image dobara de (same bytes) -> woh post nahi gini jati, alag message mein dobara.
- Ek file = EK message (saari posts, 1-slide Instagram posts bhi). ALAG: story, product mockups, before/after comparison,
  aur Instagram link ki KAI slides -> apna carousel (har slide alag image, saari slides ek carousel message).
- SUMMARY naya: upar ginti (poori / masla / aap ke kaam), phir "MASLA WALI FILES" mein har post:
  doc ka number + [script ka naam] + «heading» + wajah + "-> kya karna hai". Phir "AAP KHUD BANAO" (story/reel), phir poori files.
- "Video link:" post ke neeche -> woh post REEL (summary mein reel), "samajh nahi aayi" nahi.  "Line:" bhi heading label.

DASHBOARD (2026-10-18)
----------------------
- Server chalte hi: http://127.0.0.1:8765/   (ya OPEN_DASHBOARD.bat). Automation waise hi chalti hai — dashboard sirf record/control.
- AAJ: abhi kaun si file ban rahi hai, aaj ki posts/stories/carousel/mockups/reels, har file ka hal + masle (post number + heading + kya karna hai),
  alerts (Windows popup bhi pehle ki tarah), live activity.
- LINE MEIN FILES: jo Google Docs khuli hain SAB banti hain. Jo nahi banani us par "Skip" (chalti file bhi: agle safe point par chhod
  kar agli file; ban chuki images mehfooz; DONE nahi hoti, agli START par phir aayegi). "Wapis lao" se skip hatao.
- HISTORY: date-wise record — har din, har company, har file: kitni posts / stories / carousel slides / mockups + reels (aap ne banani).
  Purane din Output\history.json mein mehfooz (7 din wali safai ke baad bhi).
- LIVE LOG: filter (sab / masle / done) + search.  ALERTS: saare notifications.
- Buttons: START / RESUME, STOP-Pause, menu: Server restart (naya code load — pehle STOP), Server band.
- Har file ka record: Output\<date>\<file>\file_report.json

GMAIL LIST (2026-10-18) — so jao, files khud khulengi
-----------------------------------------------------
- Dashboard (Aaj) > "Gmail list": company naam add karo (kai ho to comma se) -> switch ON.
- Har 30 second Designer profile ki Gmail mein search: has:attachment filename:docx newer_than:1d (config gmail_query).
  List wali company ki email (subject / sender / file naam mein company) -> .docx par "Edit with Google Docs" -> Docs ki nayi tab.
- Server khali ho to file khulte hi automation khud START (config gmail_autostart). Chalte run mein nayi tab khud line mein.
- Asal automation par asar nahi: scan sirf tab jab koi file ban na rahi ho aur run PAUSE na ho. Apni alag Gmail tab (script group).
- Har attachment ek hi dafa khulti hai (Output\gmail_watch.json yaad rakhta hai). Pehle se ban chuki / khuli file dobara nahi.
  3 dafa na khule -> popup "khud khol lo".
- Extension v3.0: Designer profile mein sirf mail.google.com ke popups allow (Edit with Google Docs nayi tab kholta hai).
- Pehli dafa: "Abhi scan karo" daba kar dekh lo file khulti hai (Gmail ka design badle to yahan bataye ga).

GMAIL LIST UPDATE (2026-10-18 raat)
-----------------------------------
- Gmail mein SIRF company ka naam search hota hai (har company alag). Email mein Drive / Docs LINK (jaise "Wear Wood 9" chip)
  ya .docx ATTACHMENT — dono khulte hain. Link: seedha Docs mein (same file id). Attachment: "Edit with Google Docs".
- Sirf sab se NAYI email (+ aaj ki emails) — purani files (Wear Wood 2..8) dobara nahi khultin. Ban chuki / khuli file nahi khulti.
- Poori list ek saath: spreadsheet ka column copy kar ke Gmail list box mein paste karo.
- List roz chalti hai: har din har company ki aaj ki nayi file. "aaj mili" = mil gayi; "intezar" = abhi nahi aayi.
- Pehli asal test: Wear Wood 9 Gmail ke Drive link se khuli aur automation ne khud bana di.
- Dashboard ka "Server restart" button theek (RESTART_SERVER.bat khud banata hai).
- Exact naam Gmail mein na mile -> naam ke hisse: "Belle Clinic London UK" -> "Belle Clinic London" -> "Belle Clinic" -> "Belle"
  (+ bina space "Eliquid"; shuru ke aam lafz "The / Copy of" hata kar). Hisse se mili email -> sirf woh file jis ke naam mein woh hissa ho.
- Sirf aakhri 5 din ki emails (config gmail_max_days). Ek company ki 2+ files hon -> PURANI pehle (Gmail mein neeche wali), phir nayi.
- Gmail read/unread: file khul gayi / tab mein khuli -> email READ rehti hai. File na khul sake aur email pehle unread thi -> wapis UNREAD.
- "Edit with Google Docs" kabhi 5-10 second baad aata hai: script ~25 second button ka aur ~45 second Docs tab ka intezar karti hai.
- Ek file 2 tabs mein khuli ho -> sirf ek dafa banti hai. Abhi khul rahi tab (URL adhoora) se run crash nahi hota.
- Gmail list: har 5 MINUTE scan (config gmail_scan_seconds = 300). Har scan se pehle Gmail poora REFRESH (nayi aayi files zaroor dikhen).
  Switch ON karte hi pehla scan foran.
