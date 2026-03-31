# 🎓 SchoolBell – System dzwonków szkolnych (Raspberry Pi)

Nowoczesny, webowy system sterowania dzwonkiem szkolnym oparty o Raspberry Pi, Flask i GPIO.  
Pozwala na pełne zarządzanie harmonogramem lekcji, ręczne sterowanie oraz zdalny dostęp przez przeglądarkę.

---

## 🚀 Funkcje

- ⏰ Automatyczne dzwonki według planu lekcji
- 🔘 Ręczne wyzwalanie dzwonka (GUI + przycisk fizyczny)
- 🌐 Panel webowy (Flask)
- 🖼️ Obsługa logo szkoły (upload)
- 👥 System użytkowników / autoryzacji
- 📅 Profile tygodniowe i nadpisania dat
- 📊 Logi i historia zdarzeń
- 🔌 Sterowanie GPIO (np. przekaźnik)
- ⚙️ Konfiguracja przez GUI (bez edycji plików)

---

## 🧰 Wymagania

- Raspberry Pi (zalecane: Zero 2 W / 3 / 4)
- Raspberry Pi OS (Lite lub Full)
- Python 3
- Nginx

---

## ⚡ Szybka instalacja (1 komenda)

```bash
wget https://raw.githubusercontent.com/D0m1n1kB/schoolbell/main/install_schoolbell_autoinstall_v2.sh \
&& chmod +x install_schoolbell_autoinstall_v2.sh \
&& sudo ./install_schoolbell_autoinstall_v2.sh
```

Z tokenem dostępu:

```bash
sudo ./install_schoolbell_autoinstall_v2.sh MojeHaslo123
```

---

## 📦 Co robi instalator

- instaluje wszystkie zależności (Python, nginx)
- tworzy katalog `/opt/schoolbell`
- tworzy i konfiguruje `app.py`
- ustawia usługę systemd (`schoolbell.service`)
- konfiguruje nginx (reverse proxy)
- ustawia limit uploadu (logo)
- wyłącza Wi-Fi powersave
- tworzy domyślny `config.json`
- obsługuje:
  - instalację
  - aktualizację
  - backup

---

## 🔁 Aktualizacja

```bash
cd /opt/schoolbell
git pull
sudo systemctl restart schoolbell
```

---

## 🌐 Dostęp do panelu

Po instalacji:

http://IP_RASPBERRY/

---

## ⚙️ Konfiguracja

Plik:

/opt/schoolbell/config.json

Najważniejsze ustawienia:

```json
"gpio": {
  "pin": 22,
  "active_level": "HIGH"
}
```

---

## 🔌 GPIO

- Sterowanie przekaźnikiem przez GPIO
- Domyślnie:
  - pin: 22
  - stan aktywny: HIGH

---

## 📁 Struktura projektu

```
/opt/schoolbell/
├── app.py
├── config.json
├── uploads/
├── logs/
├── backup/
```

---

## 🛠️ Zarządzanie usługą

```bash
sudo systemctl status schoolbell
sudo systemctl restart schoolbell
sudo journalctl -u schoolbell -n 50
```

---

## 🧠 Architektura

- Backend: Flask (Python)
- Frontend: HTML + JS
- Reverse proxy: nginx
- Hardware: GPIO (Raspberry Pi)

---

## ⚠️ Uwagi

- Upload logo działa tylko dla:
  - PNG / JPG / WEBP / SVG
- Jeśli upload nie działa:
  - sprawdź limit nginx (`client_max_body_size`)
- Przy użyciu OneDrive:
  - najpierw zapisz plik lokalnie

---

## 📌 Roadmap

- [ ] integracja z Git (auto-update)
- [ ] panel użytkowników (roles)
- [ ] API REST
- [ ] eksport/import konfiguracji
- [ ] tryb offline (cache)

---

## 👨‍💻 Autor

Projekt tworzony jako system dzwonków szkolnych oparty o Raspberry Pi.

---

## 📄 Licencja

MIT
