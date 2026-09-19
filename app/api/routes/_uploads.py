from fastapi import HTTPException, UploadFile, status


def validate_csv(file: UploadFile) -> None:
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only CSV files are supported in the MVP.",
        )
