# AWS S3 private media storage

> **Implementation snapshot:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

SHVYA AI stores normal Django `FileField` uploads in a private Amazon S3 bucket while keeping static assets and encrypted Help & Support attachments on their existing local storage paths.

## Current SHVYA infrastructure

- Application host: Hostinger VPS
- S3 bucket: `shvya-ai`
- AWS region: `ap-south-1`

Because the application is outside AWS, the Hostinger VPS must authenticate with a dedicated least-privilege IAM access key. Keep those credentials only in the server-side environment file.

## Storage separation

Use the same private bucket with separate prefixes:

- Production: `production/media/`
- Staging: `staging/media/`

The encrypted Help & Support attachment store remains on `MEDIA_ROOT` through `PrivateSupportStorage`. Static files remain on the existing Django/Nginx path.

## Bucket security

Keep S3 Block Public Access enabled and enable bucket versioning. Objects are private and SHVYA generates temporary signed URLs for authorized access.

## IAM policy

Create a dedicated IAM user such as `shvya-s3-storage`. A policy that permits both SHVYA environments inside this bucket is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListShvyaMedia",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::shvya-ai",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "production/media",
            "production/media/*",
            "staging/media",
            "staging/media/*"
          ]
        }
      }
    },
    {
      "Sid": "ReadWriteShvyaMedia",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::shvya-ai/production/media/*",
        "arn:aws:s3:::shvya-ai/staging/media/*"
      ]
    }
  ]
}
```

## Production environment

Add only to the production server-side `.env`:

```env
USE_S3_STORAGE=True
AWS_ACCESS_KEY_ID=<iam-access-key-id>
AWS_SECRET_ACCESS_KEY=<iam-secret-access-key>
AWS_STORAGE_BUCKET_NAME=shvya-ai
AWS_S3_REGION_NAME=ap-south-1
AWS_S3_MEDIA_PREFIX=production/media
AWS_QUERYSTRING_EXPIRE=900
```

## Staging environment

Add only to `/opt/shvya-ai-staging/.env.staging`:

```env
USE_S3_STORAGE=True
AWS_ACCESS_KEY_ID=<iam-access-key-id>
AWS_SECRET_ACCESS_KEY=<iam-secret-access-key>
AWS_STORAGE_BUCKET_NAME=shvya-ai
AWS_S3_REGION_NAME=ap-south-1
AWS_S3_MEDIA_PREFIX=staging/media
AWS_QUERYSTRING_EXPIRE=900
```

The same IAM user can technically access both prefixes with the policy above. Separate IAM users for staging and production are even safer if desired.

## Existing media migration

Do not migrate `.support-encrypted/` into S3. Support attachments continue to use the existing encrypted local storage.

For production, after verifying the actual persistent media path:

```bash
aws s3 sync /path/to/shvya/media/ \
  s3://shvya-ai/production/media/ \
  --exclude ".support-encrypted/*"
```

For staging use `s3://shvya-ai/staging/media/`.

## Smoke test

After the server environment is configured and the application image contains the S3 dependencies:

```bash
python manage.py shell
```

```python
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

name = default_storage.save(
    "_health/s3-smoke.txt",
    ContentFile(b"shvya-s3-ok"),
)

print(name)
print(default_storage.exists(name))
print(default_storage.url(name))

default_storage.delete(name)
```

Expected behavior:

1. The object is created under the environment-specific prefix.
2. `exists()` returns `True`.
3. `url()` returns a temporary signed S3 URL.
4. The object is deleted successfully.
5. Anonymous public access remains blocked.
