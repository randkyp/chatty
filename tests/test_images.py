from chatty.images import (
    encode_image,
    encode_image_file,
    extract_images_from_text,
)


def test_encode_image():
    res = encode_image(b"hello", "image/png")
    # b"hello" in base64 is "aGVsbG8="
    assert res == "data:image/png;base64,aGVsbG8="


def test_encode_image_file(tmp_path):
    img_file = tmp_path / "test.png"
    img_file.write_bytes(b"fake-image-bytes")

    res = encode_image_file(img_file)
    assert res is not None
    data_url, mime = res
    assert mime == "image/png"
    assert "data:image/png;base64," in data_url


def test_encode_image_file_not_found(tmp_path):
    res = encode_image_file(tmp_path / "nonexistent.png")
    assert res is None


def test_encode_image_file_not_an_image(tmp_path):
    txt_file = tmp_path / "test.txt"
    txt_file.write_bytes(b"hello")
    res = encode_image_file(txt_file)
    assert res is None


def test_extract_images_from_text(tmp_path):
    # Setup some dummy image files
    img1 = tmp_path / "image1.png"
    img1.write_bytes(b"bytes1")

    img2 = tmp_path / "image2.jpg"
    img2.write_bytes(b"bytes2")

    # Text with unquoted path, quoted path, and non-existent path
    text = f"Check this @{img1} and also @'{img2}' but ignore @/nonexistent.png"

    cleaned, images = extract_images_from_text(text)

    # Nonexistent path shouldn't be extracted or removed
    assert "ignore @/nonexistent.png" in cleaned
    # Valid paths should be removed from text
    assert str(img1) not in cleaned
    assert str(img2) not in cleaned

    assert len(images) == 2
    assert images[0]["path"] == str(img1.resolve())
    assert images[0]["mime_type"] == "image/png"
    assert images[1]["path"] == str(img2.resolve())
    assert images[1]["mime_type"] == "image/jpeg"


def test_extract_images_from_text_duplicate(tmp_path):
    img = tmp_path / "image.png"
    img.write_bytes(b"bytes")

    text = f"Here is @{img} and @{img} again"
    cleaned, images = extract_images_from_text(text)

    assert len(images) == 1  # Duplicates are excluded
    assert str(img) not in cleaned
