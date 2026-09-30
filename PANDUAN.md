# Setup Sendiri Provider "muse" di 9Router

Provider `muse` = endpoint OpenAI-compatible yang request-nya dijawab **live oleh Muse** (agent muse.ai kamu), bukan oleh provider API lain. Cara kerjanya: 9Router meneruskan request ke sebuah *bridge* kecil di VPS, bridge mengantrekan request sebagai file, lalu cron di Muse mengambil antrean itu lewat SSH dan menulis jawabannya kembali.

```
App/klien --(OpenAI API)--> 9Router --(http://127.0.0.1:8765/v1)--> bridge.py (VPS)
                                                                        |
                                                              queue/pending/*.json
                                                                        |
Muse (cron tiap 10 detik, SSH dari sandbox) --> baca pending, jawab --> queue/done/*.json
                                                                        |
                          bridge baca done --> response OpenAI --> 9Router --> klien
```

## Prasyarat

1. **VPS Ubuntu** (akses sudo), **9Router sudah terinstall dan jalan**.
2. **Tailscale** di VPS (opsional — bridge otomatis bind juga ke IP Tailscale kalau ada, buat akses langsung).
3. **Akun Muse** (muse.ai) — poller berjalan sebagai cron di agent Muse kamu.
4. **Sandbox Muse bisa SSH ke VPS** (outbound). Sandbox tidak bisa diakses inbound, jadi yang dibutuhkan hanya SSH *keluar* dari sandbox ke VPS: siapkan keypair, taruh public key di `~/.ssh/authorized_keys` user VPS. Kalau egress kamu lewat HTTP proxy, pakai `ProxyCommand` seperti contoh di bawah.

## Langkah 1 — Deploy bridge di VPS

File yang dibutuhkan (terlampir di paket ini): `bridge.py`, `muse-bridge.service`.

```bash
sudo mkdir -p /home/ubuntu/muse-bridge/queue/pending /home/ubuntu/muse-bridge/queue/done
sudo cp bridge.py /home/ubuntu/muse-bridge/bridge.py
sudo chown -R ubuntu:ubuntu /home/ubuntu/muse-bridge
sudo cp muse-bridge.service /etc/systemd/system/muse-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now muse-bridge

# Verifikasi:
curl -s http://127.0.0.1:8765/health        # harus: {"ok": true}
curl -s http://127.0.0.1:8765/v1/models      # harus: list berisi model "muse"
```

## Langkah 2 — Daftarkan provider di 9Router

Lewat dashboard 9Router:

1. Tambah provider → tipe **OpenAI-compatible chat**.
2. Nama: `Muse`, Base URL: `http://127.0.0.1:8765/v1`, prefix: `muse`.
3. Aktifkan koneksinya, lalu klik **Test Connection** — bridge menjawab probe dashboard (`"hi"`) secara instan, jadi test harus langsung hijau.
4. Buat **combo** bernama `muse` berisi model `muse/muse`.
5. Buat API key 9Router untuk pemakaian kamu sendiri.

## Langkah 3 — Pastikan sandbox Muse bisa SSH ke VPS

Test sekali dari sandbox Muse:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no \
  -o "ProxyCommand=<PROXY_CMD> %h %p" -i <PRIVATE_KEY> <USER>@<VPS_IP> "echo ok"
```

Ganti `<PROXY_CMD>` / hilangkan opsi itu kalau tidak pakai proxy, `<PRIVATE_KEY>` dengan key yang public-nya sudah dipasang di VPS.

## Langkah 4 — Buat cron poller di Muse

Minta ke Muse kamu: *"buatkan cron tiap 10 detik dengan instruksi persis seperti ini:"*

> Muse bridge poller — kamu adalah mesin penjawab untuk Provider "Muse" di 9Router.
>
> Arsitektur: 9Router -> bridge di VPS (http://127.0.0.1:8765/v1, systemd service muse-bridge) -> request ditulis ke /home/ubuntu/muse-bridge/queue/pending/\<id\>.json -> bridge menunggu jawaban di queue/done/\<id\>.json maksimal 240 detik. Tugasmu: tiap run kuras antrean — jawab SEMUA request pending (maksimal 5 per run), mulai dari yang tertua, sebagai Muse.
>
> SSH ke VPS (sesuaikan dengan setup-mu):
> `ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=no -o "ProxyCommand=<PROXY_CMD> %h %p" -i <PRIVATE_KEY> <USER>@<VPS_IP>`
>
> Langkah tiap run:
> 1. List antrean: `ssh ... "ls -tr /home/ubuntu/muse-bridge/queue/pending/ 2>/dev/null"`. Kalau kosong → tidak ada kerjaan, akhiri run TANPA pesan ke user.
> 2. Kalau ada, proses SATU PER SATU dari yang tertua, maksimal 5 file per run. Untuk setiap file:
>    a. Baca: `ssh ... "cat /home/ubuntu/muse-bridge/queue/pending/<file>"`. Isinya {"id": "...", "received_at": \<unix epoch\>, "request": {"messages": [...], "max_tokens": ..., "temperature": ...}}.
>    b. Kalau umur request > 220 detik (received_at vs waktu sekarang) → hapus file pending itu (`rm`), lanjut ke file berikutnya TANPA pesan.
>    c. Jawab SEBAGAI MUSE: baca messages untuk konteks, jawab pesan terakhir user secara natural dan membantu, dalam bahasa yang dipakai user. Hormati max_tokens bila ada. Ini jawaban model AI biasa — bukan obrolan dengan pemilik agent — jadi jangan sebut dirimu "Muse" kecuali ditanya.
>    d. Tulis jawaban ke file lokal /tmp/muse_answer.txt, lalu kirim ke VPS TANPA mengubah isinya:
>       `ssh ... "python3 -c 'import json,sys; json.dump({\"content\": sys.stdin.read()}, open(\"/home/ubuntu/muse-bridge/queue/done/<id>.json\", \"w\"))'" < /tmp/muse_answer.txt`
>       lalu `ssh ... "rm /home/ubuntu/muse-bridge/queue/pending/<file>"` dan hapus /tmp/muse_answer.txt lokal.
>    e. Lanjut ke file tertua berikutnya sampai antrean habis atau sudah 5 terjawab.
> 3. Akhiri run TANPA pesan ke user bila sukses.
>
> Kirim pesan ke user HANYA bila ada yang rusak: SSH gagal 2 run berturut-turut, bridge tidak merespons /health, atau antrean pending menumpuk > 4 file.

## Langkah 5 — Verifikasi end-to-end

```bash
curl https://<HOST_9ROUTER>/v1/chat/completions \
  -H "Authorization: Bearer <API_KEY_9ROUTER>" \
  -H "Content-Type: application/json" \
  -d '{"model":"muse","messages":[{"role":"user","content":"Halo, siapa kamu?"}]}'
```

Harusnya dibalas dalam ~10–20 detik oleh Muse.

## Catatan penting

- **Latency** ~10–20 detik karena poller jalan tiap 10 detik. Interval bisa dirapatkan/dilonggarkan, tapi tiap run adalah agent run → makin sering makin boros kuota.
- **Kuota:** request yang dijawab poller TIDAK memotong kuota provider 9Router mana pun, tapi **memotong kuota Muse (free weekly)** milik pemilik agent.
- Bridge menunggu jawaban maksimal **240 detik**; request berumur > 220 detik dibuang poller.
- Maksimal **5 request pending**; lebih dari itu bridge balas HTTP 429 ("busy").
- **Akses langsung tanpa 9Router:** `http://<IP_TAILSCALE_VPS>:8765/v1` — hanya dari dalam tailnet.
- **Streaming didukung** (SSE, dengan keepalive agar proxy tidak timeout).

## Troubleshooting

| Gejala | Kemungkinan penyebab |
|---|---|
| Dashboard "Test Connection" gagal | Bridge tidak jalan / port salah — cek `systemctl status muse-bridge` dan `/health` |
| HTTP 504 "did not answer in time" | Poller tidak jalan atau SSH gagal — cek cron Muse & koneksi SSH |
| HTTP 429 "bridge busy" | Antrean penuh (5 pending) — tunggu, atau cek poller macet |
| Jawaban lama sekali | Interval poller terlalu jarang, atau VPS–sandbox lambat |
