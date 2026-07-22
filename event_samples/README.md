# CALL_HISTORY — samples dos eventos emitidos

Um arquivo por status que o gateway emite para a fila **CallHistoryQueue**
(body SQS = o JSON cru do arquivo, sem envelope SNS). O código emissor é
`lk_ultravox_bridge/call_history.py`; o teste
`tests/unit/test_call_history.py` falha se esta pasta e o código divergirem
(status a mais, a menos, ou shape errado) — é o tracking do modelo.

## Sequências por cenário

| Cenário | Sequência emitida |
|---|---|
| Chamada atendida e concluída | `CALL_ATTEMPT_STARTED` → `SIP_DIAL_ANSWERED` → `SIP_BRIDGE_ACTIVE` → `SIP_CALL_ENDED` |
| Não atendida (no-answer, busy, declined, unavailable, invalid-number, dial-timeout) | `CALL_ATTEMPT_STARTED` → `CALL_NOT_ANSWERED` (mensagem SQS ackada, sem retry) |
| Erro de sistema antes do atendimento (Ultravox REST, trunk, rede) | [`CALL_ATTEMPT_STARTED` →] `SIP_CALL_FAILED` (mensagem volta à fila → nova tentativa emite nova sequência) |
| Bridge morre depois do atendimento | `CALL_ATTEMPT_STARTED` → `SIP_DIAL_ANSWERED` → `SIP_BRIDGE_ACTIVE` → `SIP_CALL_ENDED` com `endReason=bridge-error` (a chamada aconteceu; talk time é real; já ackada, sem retry) |

Os quatro primeiros samples contam a mesma chamada (mesmo `callId`);
`CALL_NOT_ANSWERED` e `SIP_CALL_FAILED` são chamadas distintas.

## metadataJson

**Base comum a todos os eventos** — contexto que só o gateway conhece:

| Campo | Significado |
|---|---|
| `room` | Sala LiveKit da chamada; chave de correlação com os logs no Loki (`[call=… room=…]`) |
| `toNumber` | Qual número da lista `phoneNumbers` foi efetivamente discado |
| `country` / `provider` | Perna SIP que carregou a chamada (`BR`/`twilio`, `CL`/`switch`) |

**Por evento, além da base:**

- `SIP_DIAL_ANSWERED`: `answerDelaySeconds` (int, do dial ao atendimento).
- `SIP_CALL_ENDED`: `durationSeconds` (int, do atendimento ao fim do bridge —
  alimenta talk time), `endReason` e `ultravoxCallId` (correlaciona com
  gravação/transcript no Ultravox; ids de fora da nossa fronteira ficam
  sempre aqui, nunca no `metadata` estruturado).
- `CALL_NOT_ANSWERED`: `reason`, `sipStatus`.
- `SIP_CALL_FAILED`: `reason`, `errorType` (classe da exceção), `attempt`
  (`ApproximateReceiveCount` da entrega SQS) e `sipStatus` quando a falha
  carrega um código SIP não mapeado (ex.: 5xx do trunk).
- **Regra do `sipStatus`** (retorno Digicob lê daqui): presente somente
  quando existe código SIP real no cenário; sem código (ex.: `dial-timeout`,
  erro de rede antes do INVITE), a chave é **omitida** — nunca inventada.
  Chamada atendida não tem código de falha, então `SIP_CALL_ENDED` não
  carrega `sipStatus` (200 implícito).

## Observações de contrato

- `CALL_NOT_ANSWERED` é **status novo** (fora do enum atual do consumidor):
  persiste com warn até o de-para aprendê-lo. Desfecho de negócio — o
  redial pertence ao sistema de campanha, nunca ao visibility timeout.
- `SIP_CALL_FAILED` só é emitido **antes** do atendimento (erro retryable). O
  retry é limitado pela redrive policy da `TriggerCallQueue`
  (`maxReceiveCount=5` → `TriggerCallDLQ`, verificado em 2026-07-21), então
  cada mensagem emite no máximo 5 `SIP_CALL_FAILED` — o campo `attempt`
  distingue as tentativas.
- Mensagem que falha no **parse** (JSON inválido, `messageType` errado, sem
  número) não emite evento nenhum — não há `callId` para emitir. Ela cai na
  `TriggerCallDLQ` após 5 tentativas; a DLQ precisa de alarme próprio no
  CloudWatch (`ApproximateNumberOfMessagesVisible > 0`). No Grafana, o
  painel "Falhas de processamento por tipo" separa payload inválido
  (`ValueError`/`JSONDecodeError`) de erro transitório, e o stat "A caminho
  da DLQ" acusa falhas na 4ª+ entrega.
- `metadataJson` é **string** com JSON dentro (double-encoding), conforme o
  contrato.
- Valores de `endReason` (kebab-case, mesmos do log `audio bridge finished`
  e do painel Grafana): `callee-hangup` (cliente desligou no telefone),
  `ultravox-closed` (lado agente encerrou o WS — inclui hangUp de voicemail),
  `silence-watchdog` (Ultravox mudo ≥30s), `sip-audio-ended`, `room-lost`,
  `bridge-error`, `unknown`.
- Valores de `reason` do `CALL_NOT_ANSWERED`: `no-answer` (chamou até cair,
  SIP 408), `busy` (486/600), `declined` (recusou no botão, 603),
  `unavailable` (desligado/sem cobertura, 480), `invalid-number` (404/484),
  `dial-timeout` (guarda de 90s — API travou).
- Ids (`organizationId`, `tenantId`, `workflowId`, `campaignId`,
  `customerId`, `userId`, `callId`) são **ecoados do TRIGGER_CALL** com a
  mesma resolução usada no metadata do Ultravox (`callId`:
  `metadata.callId` → `payload.callId` → `payload.id`). `userId` vai como
  veio na mensagem.
- Emissão é best-effort (nunca bloqueia nem derruba uma chamada viva);
  ordenação no consumidor via `createdAt` + `id` UUIDv7 (time-ordered).
- Publicação é opt-in: `CALL_HISTORY_QUEUE_NAME` vazio = desligado.
