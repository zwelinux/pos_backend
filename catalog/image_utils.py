from PIL import Image
from io import BytesIO
import os
from django.core.files.base import ContentFile

def process_product_image(image_file, max_size=(1024, 1024), quality=85):
    """
    Resizes the image and converts it to WebP.
    """
    try:
        img = Image.open(image_file)
        
        # Convert to RGB if it's RGBA or P (to avoid transparency issues in WebP if not needed)
        # Note: If you want to keep transparency, skip the convert("RGB") but WebP supports it.
        # But for product images, RGB is usually safer for background consistency.
        if img.mode in ("RGBA", "P"):
            # If transparency is important, you can keep RGBA. 
            # If you want to flatten it to white background:
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.split()[3])
            else:
                background.paste(img)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize using thumbnail (preserves aspect ratio)
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Save to buffer as WebP
        buffer = BytesIO()
        img.save(buffer, format="WEBP", quality=quality, optimize=True)
        
        # Extract filename without extension
        filename = os.path.splitext(image_file.name)[0]
        new_filename = f"{filename}.webp"
        
        return ContentFile(buffer.getvalue(), name=new_filename)
    except Exception as e:
        print(f"Error processing image: {e}")
        return image_file # Fallback to original if something fails
