# Possíveis Melhorias

## 1. AGC (Automatic Gain Control) no caminho Ultravox → LiveKit

**Status:** Planejado, não implementado. Desnecessário após correção do sample rate para 16kHz (2026-04-14).

**Problema que resolve:** Se houver variação perceptível de volume na voz do agente (TTS Ultravox produzindo amostras com amplitude irregular), o AGC normaliza o RMS de cada frame de áudio para um nível alvo.

**Quando implementar:** Somente se a variação de volume voltar a ser um problema mesmo com `SAMPLE_RATE=16000`.

### Abordagem

Normalização RMS por frame com gain suavizado via média móvel exponencial (EMA), aplicada em `_ultravox_to_livekit` (`audio_bridge.py`) antes de copiar o chunk para o `AudioFrame`.

### Parâmetros (env vars)

| Env Var | Default | Descrição |
|---------|---------|-----------|
| `AGC_ENABLED` | `true` | Liga/desliga sem redeploy |
| `AGC_TARGET_RMS` | `3000` | RMS alvo (~-20.8 dBFS, nível confortável para telefonia) |
| `AGC_SMOOTHING_MS` | `200` | Constante de tempo da EMA (~10 frames a 20ms/frame) |
| `AGC_RMS_FLOOR` | `10` | RMS abaixo disso = silêncio, mantém gain anterior |

### Arquivos afetados

- `lk_ultravox_bridge/config.py` — 4 campos novos no `BridgeConfig`
- `lk_ultravox_bridge/audio_bridge.py` — função `_apply_agc()` + integração no loop de frames

### Lógica da função `_apply_agc`

```python
import numpy as np

def _apply_agc(
    pcm_bytes: bytes | bytearray,
    target_rms: float,
    rms_floor: float,
    alpha: float,
    prev_gain: float,
) -> tuple[bytes, float]:
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    rms = np.sqrt(np.mean(samples * samples))

    if rms < rms_floor:
        return pcm_bytes, prev_gain

    desired_gain = target_rms / rms
    gain = prev_gain + alpha * (desired_gain - prev_gain)

    amplified = samples * gain
    amplified = np.clip(amplified, -32768, 32767)

    return amplified.astype(np.int16).tobytes(), gain
```

### Pontos de integração em `_ultravox_to_livekit`

1. **Inicialização:** calcular `alpha = 1 - exp(-frame_ms / smoothing_ms)`, inicializar `agc_gain = 1.0`
2. **Loop de frames:** entre extrair o chunk do buffer e copiar pro `AudioFrame`, chamar `_apply_agc` se habilitado
3. **Barge-in (`playbackClearBuffer`):** resetar `agc_gain = 1.0` para que a nova utterance não herde gain da anterior
4. **Log periódico:** incluir `agcGain=X.XX` no log de 2s

### Performance estimada

- 320 amostras/frame a 16kHz (640 bytes) — operações numpy vetorizadas em ~5-10μs
- Frame period = 20.000μs — overhead < 0.05%
- Quando desabilitado (`AGC_ENABLED=false`): zero overhead

### Edge cases

- **Gain runaway:** se o áudio tiver RMS muito baixo por tempo prolongado, o gain pode crescer excessivamente. Adicionar `max_gain=10.0` como cap de segurança.
- **Cold start:** gain inicial em 1.0 converge para o valor correto em ~200ms (imperceptível).
- **Multi-channel:** funciona para mono (padrão SIP). Para stereo, o gain seria aplicado igualmente nos dois canais.

---

## 2. Reduzir `clientBufferSizeMs` ou ajustar conforme sample rate

**Status:** A investigar.

**Contexto:** O parâmetro `clientBufferSizeMs` enviado ao Ultravox na criação da chamada (`ultravox_client.py`) está em 60ms. Com a mudança para 16kHz, o tamanho dos chunks enviados pelo Ultravox pode ter mudado. Vale investigar se aumentar para 100-150ms melhora a regularidade dos chunks recebidos.

**Arquivo:** `lk_ultravox_bridge/ultravox_client.py`, campo `clientBufferSizeMs` no payload da chamada.

---

## 3. Jitter buffer dinâmico

**Status:** Não necessário atualmente (zero drops observados em logs de produção com 16kHz).

**Problema que resolve:** O jitter buffer atual tem tamanho fixo (`MAX_BUFFER_FRAMES=5`, 100ms). Se condições de rede variarem, um buffer adaptativo que cresce/encolhe conforme o jitter observado poderia oferecer melhor trade-off entre latência e resiliência.

**Quando implementar:** Se logs mostrarem `droppedTotal > 0` com frequência em chamadas reais.
