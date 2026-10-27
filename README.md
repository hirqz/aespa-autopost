# aespa autopost — versi Windows

Chrome membuka **x.com → tab Following** dengan akun X cadangan setiap ~5 menit,
lalu tweet bermedia tentang karina/winter (foto resolusi asli, video, teks)
dikirim ke topic 18 di grup Telegram.

## Persiapan (sekali saja)

1. **Python 3.10+** — unduh dari python.org. Saat instal, centang **"Add python.exe to PATH"**.
2. **Akun X cadangan** — jangan akun utama. Dari akun itu, **follow ketujuh akun** yang ada
   di `config.py` (aespa_official, aespasbbl, karinaspics, winterpic, ujiminphoria,
   snowdrop_010101, sunshine_080507).
3. Ekstrak folder ini ke lokasi tetap, misalnya `C:\aespa-autopost`.
4. Klik dua kali **`setup.bat`** → tunggu sampai selesai.
5. Buka **`secrets.json`** dengan Notepad → isi `BOT_TOKEN` → Save.
6. Klik dua kali **`login.bat`** → Chrome terbuka → login akun X cadangan →
   setelah timeline muncul, kembali ke jendela hitam dan tekan **Enter**.
7. Klik dua kali **`test.bat`** → pesan "✅ Bot aktif (Windows)." masuk ke topic 18.
8. Klik dua kali **`run.bat`** → biarkan jendela hitam & Chrome tetap terbuka
   (boleh di-minimize, **jangan ditutup**).

Pengecekan pertama hanya menandai tweet yang sudah ada; tweet **baru** terkirim
mulai pengecekan berikutnya.

## Supaya jalan 24 jam

- **Jangan sleep**: Settings → System → Power → *Screen and sleep* → "When plugged in, put my device to sleep after" = **Never**.
  Layar boleh mati, yang penting PC tidak sleep.
- **Jalan otomatis setelah restart**: tekan `Win + R` → ketik `shell:startup` → Enter →
  buat shortcut `run.bat` di folder itu (klik kanan run.bat → Show more options → Create shortcut → pindahkan).
- **Windows Update**: atur *Active hours* supaya restart otomatis tidak terjadi di jam ramai.
- Kalau akun X cadangan logout, bot mengirim peringatan ⚠️ ke topic 18. Jalankan `login.bat` lagi.

## Sehari-hari

| Mau apa | Caranya |
|---|---|
| Tambah akun | tambahkan di `ACCOUNTS` (config.py) **dan follow** dari akun cadangan |
| Tambah kata kunci / emoji / idol lain | edit `config.py` |
| Terapkan perubahan config | tutup jendela run.bat → buka lagi |
| Lihat riwayat | buka `autopost.log` |
| Mulai ulang dari tweet terbaru | `python autopost.py reset` |
