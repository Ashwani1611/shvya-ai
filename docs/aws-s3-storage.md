# AWS S3 private media storage

SHVYA AI can store normal Django `FileField` uploads in a private Amazon S3 bucket while keeping static assets and encrypted Help & Support attachments on their existing local storage paths.

## What moves to S3

When `USE_S3_STORAGE=True`, Django's default storage backend becomes `storages.backends.s3.S3Storage`. Existing features that rely on Django's default storage, including AI knowledge documents, calendar uploads, hosted media, and generated sales files, can use S3 without storing AWS credentials in the database.

The encrypted Help & Support attachment store remains on `MEDIA_ROOT` through `PrivateSupportStorage`. Static files remain on the existing Django/Nginx path.

## Recommended buckets

Use different buckets or isolated prefixes/roles for staging and production.

- Production: `shvya-ai-production-files`
- Staging: `shvya-ai-staging-files`
- Region: `ap-south-1`

Keep S3 Block Public Access enabled.

## Bucket bootstrap

Run with an AWS administrator/operator identity. Change the bucket names if they are already taken globally.

```bash
aws s3api create-bucket \
  --bucket shvya-ai-production-files \
  --region ap-south-1 \
  --create-bucket-configuration LocationConstraint=ap-south-1

aws s3api put-public-access-block \
  --bucket shvya-ai-production-files \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-versioning \
  --bucket shvya-ai-production-files \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket shvya-ai-production-files \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":false}]}'
```

Repeat for the staging bucket.

## IAM role for the SHVYA application

Prefer an EC2 instance role instead of long-lived `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` values.

Example production policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListShvyaMedia",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::shvya-ai-production-files",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["media", "media/*"]
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
      "Resource": "arn:aws:s3:::shvya-ai-production-files/media/*"
    }
  ]
}
```

Use a separate policy/role for staging.

## Django environment

Production:

```env
USE_S3_STORAGE=True
AWS_STORAGE_BUCKET_NAME=shvya-ai-production-files
AWS_S3_REGION_NAME=ap-south-1
AWS_S3_MEDIA_PREFIX=media
AWS_QUERYSTRING_EXPIRE=900
```

Staging:

```env
USE_S3_STORAGE=True
AWS_STORAGE_BUCKET_NAME=shvya-ai-staging-files
AWS_S3_REGION_NAME=ap-south-1
AWS_S3_MEDIA_PREFIX=media
AWS_QUERYSTRING_EXPIRE=900
```

No application access keys are required when the host/container can assume the EC2 role. boto3 uses the AWS credential provider chain automatically.

## Existing media migration

Do not copy the private support attachment directory into the default S3 media path as part of this migration. It continues to be managed by `PrivateSupportStorage`.

From the persistent media volume, migrate normal media objects while excluding that directory:

```bash
aws s3 sync /path/to/shvya/media/ \
  s3://shvya-ai-production-files/media/ \
  --exclude ".support-encrypted/*"
```

Verify the actual persistent media path before running this command.

Because Django stores relative object names such as `ai_knowledge/2026/09/file.pdf` in PostgreSQL, copying those same names under the configured `media/` S3 prefix lets existing database rows resolve through the new backend without rewriting those rows.

## Smoke test

After the environment is configured and the application image contains the S3 dependencies:

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

1. The object is created under `media/_health/`.
2. `exists()` returns `True`.
3. `url()` returns a time-limited signed S3 URL.
4. The object is deleted successfully.
5. Direct anonymous access to the bucket remains blocked.

## Rollout order

1. Create and secure the staging bucket.
2. Attach the staging IAM role.
3. Set the staging environment variables.
4. Deploy the S3 code to staging.
5. Run the smoke test and upload/download/delete tests from AI Knowledge and Calendar.
6. Copy existing normal staging media if required.
7. Create and secure the production bucket.
8. Attach the production IAM role.
9. Copy existing production normal media, excluding `.support-encrypted/`.
10. Enable the production environment variables and deploy.
