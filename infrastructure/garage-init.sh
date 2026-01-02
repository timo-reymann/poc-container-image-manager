#!/bin/bash
set -e

GARAGE_ADMIN="http://garage:3903"
BUCKET_NAME="buildkit-cache"
KEY_NAME="buildkit-key"
# Fixed credentials that match what we'll put in building.py
ACCESS_KEY_ID="GKbuildkit00000000000000000000000"
SECRET_KEY="buildkitsecret000000000000000000000000000000"

echo "Waiting for Garage admin API..."
until curl -sf "${GARAGE_ADMIN}/health" > /dev/null 2>&1; do
    sleep 1
done
echo "Garage is ready"

# Get cluster status and node ID
echo "Getting cluster status..."
CLUSTER_STATUS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/status")
NODE_ID=$(echo "$CLUSTER_STATUS" | jq -r '.node')

# Check if layout already configured
LAYOUT=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/layout")
STAGED_COUNT=$(echo "$LAYOUT" | jq '.stagedRoleChanges | length')

if [ "$STAGED_COUNT" -eq 0 ]; then
    ROLES=$(echo "$LAYOUT" | jq '.roles | length')
    if [ "$ROLES" -eq 0 ]; then
        echo "Configuring node layout..."
        curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
            -H "Content-Type: application/json" \
            -d "{\"$NODE_ID\": {\"zone\": \"dc1\", \"capacity\": 1073741824, \"tags\": []}}" \
            "${GARAGE_ADMIN}/v1/layout"

        echo "Applying layout..."
        CURRENT_VERSION=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/layout" | jq '.version')
        curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
            -H "Content-Type: application/json" \
            -d "{\"version\": $((CURRENT_VERSION + 1))}" \
            "${GARAGE_ADMIN}/v1/layout/apply"
    fi
fi

# Check if bucket exists
echo "Checking bucket..."
BUCKETS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/bucket?list")
BUCKET_EXISTS=$(echo "$BUCKETS" | jq -r ".[] | select(.globalAliases[]? == \"$BUCKET_NAME\") | .id")

if [ -z "$BUCKET_EXISTS" ]; then
    echo "Creating bucket ${BUCKET_NAME}..."
    BUCKET_RESULT=$(curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
        -H "Content-Type: application/json" \
        -d "{\"globalAlias\": \"$BUCKET_NAME\"}" \
        "${GARAGE_ADMIN}/v1/bucket")
    BUCKET_ID=$(echo "$BUCKET_RESULT" | jq -r '.id')
else
    BUCKET_ID="$BUCKET_EXISTS"
fi
echo "Bucket ID: $BUCKET_ID"

# Check if key exists
echo "Checking access key..."
KEYS=$(curl -sf -H "Authorization: Bearer admin-token-for-local-dev" "${GARAGE_ADMIN}/v1/key?list")
KEY_EXISTS=$(echo "$KEYS" | jq -r ".[] | select(.name == \"$KEY_NAME\") | .id")

if [ -z "$KEY_EXISTS" ]; then
    echo "Creating access key ${KEY_NAME}..."
    curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"$KEY_NAME\", \"accessKeyId\": \"$ACCESS_KEY_ID\", \"secretAccessKey\": \"$SECRET_KEY\"}" \
        "${GARAGE_ADMIN}/v1/key/import"
fi

# Grant bucket permissions to key
echo "Granting bucket permissions..."
curl -sf -X POST -H "Authorization: Bearer admin-token-for-local-dev" \
    -H "Content-Type: application/json" \
    -d "{\"bucketId\": \"$BUCKET_ID\", \"accessKeyId\": \"$ACCESS_KEY_ID\", \"permissions\": {\"read\": true, \"write\": true, \"owner\": true}}" \
    "${GARAGE_ADMIN}/v1/bucket/allow"

echo "Garage initialization complete!"
echo "  Bucket: $BUCKET_NAME"
echo "  Access Key ID: $ACCESS_KEY_ID"
