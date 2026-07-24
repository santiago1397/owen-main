#!/usr/bin/env bash
# Render an asterisk/*.conf TEMPLATE from .env.prod.
#
# WHY THIS EXISTS: a bare `envsubst` substitutes EVERY ${...} token it can resolve as a
# shell variable — including Asterisk's OWN dialplan variables. That silently corrupted the
# deployed extensions.conf: ${EXTEN} and ${BULKVS_FROM} are Asterisk runtime variables, not
# deploy-time env vars, so they rendered EMPTY and produced dead lines like
#     Dial(PJSIP/@bulkvs,60)          <- destination gone
#     Set(CALLERID(num)=)             <- caller-ID gone
# (${CALLERID(num)} survived only because it isn't a valid shell identifier.)
#
# envsubst takes an explicit SHELL-FORMAT allowlist; anything not listed is left verbatim.
# That is the whole fix — keep DEPLOY_VARS in sync when a template gains a new ${VAR}.
#
# Usage:  set -a; . /opt/santiagoproperties/owen-main/.env.prod; set +a
#         asterisk/render.sh extensions > /tmp/extensions.conf
set -euo pipefail

# Deploy-time substitutions ONLY. Asterisk runtime variables (EXTEN, CALLERID(num),
# BULKVS_FROM, and any other dialplan variable) MUST NOT appear here.
DEPLOY_VARS='
$ARI_APP $ARI_BIND_ADDR $ARI_HOST $ARI_PASSWORD $ARI_PORT $ARI_USERNAME
$ASTERISK_CDR_DB_PASSWORD $ASTERISK_CDR_DB_USER $ASTERISK_PUBLIC_IP
$BULKVS_FROM_NUMBER $BULKVS_SIP_PASSWORD $BULKVS_SIP_USERNAME $BULKVS_TRUNK_NAME
$OPERATOR_SIP_DOMAIN $OPERATOR_SIP_SECRET $OPERATOR_SLUG_EXAMPLE
$POSTGRES_DB $POSTGRES_HOST $POSTGRES_PORT
$STUN_SERVER $TURN_STATIC_SECRET $TURN_TLS_CERT $TURN_TLS_KEY
'

name="${1:?usage: render.sh <pjsip|ari|http|rtp|extensions|cdr|cdr_pgsql|turnserver>}"
src="$(dirname "$0")/${name}.conf"
test -f "$src" || { echo "render.sh: no such template: $src" >&2; exit 1; }

envsubst "$DEPLOY_VARS" < "$src"
