# aespa autopost — X ke Telegram

Memantau beberapa akun X tiap 5 menit dan mengirim tweet bermedia tentang
karina/winter (foto resolusi asli + video + teks) ke topic Telegram.
Berjalan gratis di GitHub Actions.

## Isi repo

| File | Fungsi |
|---|---|
| `config.py` | **Pengaturan**: akun, kata kunci member, rule per akun, topic |
| `x_to_telegram.py` | Script utama |
| `.github/workflows/autopost.yml` | Jadwal tiap 5 menit |
| `state.json` | Posisi tweet terakhir per akun (diperbarui otomatis oleh bot) |

## Pemasangan

1. **Secrets**: Settings → Secrets and variables → Actions → *New repository secret*
   - `BOT_TOKEN` = token dari @BotFather
   - `CHAT_ID` = `-1002820523850`
2. **Izin tulis**: Settings → Actions → General → *Workflow permissions* →
   pilih **Read and write permissions** → Save.
3. **Uji**: tab **Actions** → *Auto post X ke Telegram* → **Run workflow**
   - mode `diagnose` → cek apakah X bisa diakses (cari `HTTP 200` dan `tweet: >0`)
   - mode `test` → pesan "✅ Bot aktif" masuk ke topic 18
4. Selesai. Jadwal tiap 5 menit berjalan sendiri. Run pertama tiap akun hanya
   menyimpan posisi (tidak memposting); tweet baru terkirim mulai run berikutnya.

## Sehari-hari

- **Tambah akun / kata kunci / rule**: edit `config.py` di GitHub (ikon pensil) → *Commit changes*.
- **Lihat log**: tab Actions → klik run → *autopost* → *Jalankan*.
- **Mulai ulang dari tweet terbaru**: Run workflow dengan mode `reset`.

## Catatan

- GitHub sering menjalankan jadwal terlambat beberapa menit; itu normal.
- Jadwal otomatis dinonaktifkan GitHub kalau repo tidak ada aktivitas 60 hari.
  Bot meng-commit `state.json` setiap ada perubahan, jadi biasanya tetap aktif;
  kalau ternyata berhenti, cukup klik *Enable workflow* di tab Actions.
