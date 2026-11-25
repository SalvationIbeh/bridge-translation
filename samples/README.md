# Test Audio

Put fake Spanish test audio here for the Stage 1 spike.

Supported starting points:

- `spanish_test.wav`
- `spanish_test.mp3`
- `spanish_test.m4a`

Do not use customer calls or banking data. A good first phrase is:

`Quiero bloquear mi tarjeta porque vi un cargo extrano.`

## Benchmark Set

The benchmark manifest is `stage1-benchmark.json`. The long baseline sample already
uses `spanish_test.mp3`. Record these short fake samples next:

| File | Phrase |
|---|---|
| `short_01_card_block.mp3` | `Quiero bloquear mi tarjeta por un cargo extrano.` |
| `short_02_balance_check.mp3` | `Necesito revisar el saldo de mi cuenta.` |
| `short_03_payment_rejected.mp3` | `Mi pago fue rechazado tres veces.` |
| `short_04_transfer_pending.mp3` | `Mi transferencia sigue pendiente desde ayer.` |
| `short_05_card_arrival.mp3` | `Cuando llegara mi nueva tarjeta.` |

Keep each recording short and natural. One utterance per file is enough.

For latency experiments, the files can also be generated synthetically from the
manifest with OpenAI TTS:

```powershell
python .\scripts\generate_stage1_samples.py
```

Synthetic clips are convenient for repeatable timing. Human recordings are still
needed before judging accent coverage or real caller behavior.
