# YouTube → Shorts Generator (निजी इस्तेमाल के लिए)

वीडियो लिंक पेस्ट करो → वह अपने-आप 30-60 सेकंड की, 9:16 वर्टिकल, कैप्शन लगी क्लिप्स बना देता है।

## क्या-क्या करता है
- YouTube (या डायरेक्ट MP4 लिंक) से 480p में वीडियो डाउनलोड करता है
- अगर subtitle/transcript मिल जाए, तो "दिलचस्प" हिस्से खुद चुनता है (सवाल, हुक-वर्ड्स, घनी बातचीत वाले हिस्से)
- हर क्लिप को 1080x1920 (9:16) में क्रॉप करता है
- ट्रांसक्रिप्ट से ऑटो-कैप्शन बर्न करता है
- ज़्यादा से ज़्यादा 5 क्लिप, 15 मिनट तक के वीडियो — ताकि सर्वर क्रैश न हो

## लोकल पर टेस्ट करना (डिप्लॉय करने से पहले)
```
pip install -r requirements.txt
python app.py
```
फिर ब्राउज़र में `http://localhost:5000` खोलें।

## Render पर डिप्लॉय करना
1. यह पूरा फोल्डर एक **Private** GitHub repository में डालें (नीचे "GitHub पर डालना" देखें)
2. https://render.com पर जाकर "New Web Service" चुनें, अपनी repo जोड़ें
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --timeout 300 --workers 1`
5. Deploy होने के बाद जो URL मिले, वही आपकी वेबसाइट है

## GitHub पर डालना
```
git init
git add .
git commit -m "first version"
git branch -M main
git remote add origin <अपनी private repo का URL>
git push -u origin main
```
`.gitignore` पहले से `cookies.txt`, `downloads/`, `clips/`, और `.env` को बाहर रखता है — इन्हें कभी कमिट न करें।

## अगर "YouTube bot verification" वाला error आए
1. Chrome में YouTube से लॉगिन रहते हुए "Get cookies.txt LOCALLY" एक्सटेंशन से cookies.txt एक्सपोर्ट करें
2. वेबसाइट के फॉर्म में cookies फाइल अपलोड करके फिर कोशिश करें
3. cookies कुछ दिन/हफ्तों बाद एक्सपायर हो सकती हैं — तब दोबारा एक्सपोर्ट करें

## सीमाएं (जानबूझकर, ताकि फ्री टियर पर स्थिर रहे)
- एक बार में सिर्फ एक वीडियो प्रोसेस होता है
- 15 मिनट से लंबे वीडियो सपोर्ट नहीं होंगे
- अधिकतम 5 क्लिप प्रति वीडियो
- फाइलें 1 घंटे बाद अपने-आप डिलीट हो जाती हैं
- हाइलाइट चुनना एक सिंपल स्कोरिंग सिस्टम से होता है (paid AI tools जितना सटीक नहीं, पर काफी करीब)
