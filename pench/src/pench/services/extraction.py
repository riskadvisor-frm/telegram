import logging
import os
import re
from typing import Any, Optional
from urllib.parse import unquote

import cv2

logger = logging.getLogger(__name__)


class UPIExtractionService:
    """Service for extracting payment details from various sources."""

    UPI_PATTERN = re.compile(
        r"(?<![A-Za-z0-9._-])[A-Za-z0-9][A-Za-z0-9._-]{1,254}[A-Za-z0-9]@[A-Za-z]{2,64}(?![A-Za-z0-9.])",
        re.IGNORECASE,
    )
    INVALID_UPI_IDS = {
        "support@lobby",
        "disable-devtool@latest",
    }

    def __init__(self):
        """Initialize the service and check for optional dependencies."""
        pass

    def _is_valid_upi(self, upi: str) -> bool:
        """Filter out invalid UPI IDs."""
        if upi.lower() in self.INVALID_UPI_IDS:
            return False
            
        if '@webp' in upi.lower():
            return False
        # Check for suspicious repeated characters(e.g. 0000@000, aaaa@aaa, xxxxxx@xxx, abababab@aba etc.)
        username = upi.split('@')[0]
        if len(set(username.replace('.', '').replace('_', '').replace('-', ''))) <= 2:
            return False
        return True

    def _extract_upi_from_text(self, text: str) -> Optional[str]:
        """Extract UPI IDs from any text content using regex patterns."""
        try:
            # Find all UPI ID matches
            upi_matches = self.UPI_PATTERN.findall(unquote(text))

            if upi_matches:
                # Remove duplicates while preserving order
                unique_upis = list(dict.fromkeys(upi_matches))
                # Filter out invalid UPIs
                valid_upis = [upi for upi in unique_upis if self._is_valid_upi(upi)]
                
                if valid_upis:
                    logger.info(f"Found {len(valid_upis)} valid UPI ID(s): {valid_upis}")
                    return valid_upis[0]
                
                logger.debug(f"Found {len(unique_upis)} UPI(s) but all were invalid: {unique_upis}")
            else:
                logger.debug("No UPI IDs found in text")
                return None

        except Exception as e:
            logger.error(f"Error extracting UPI from text: {str(e)}")
            return None

    def extract_upi_from_html(self, html_content: str) -> Optional[str]:
        """Extract UPI IDs from HTML content using regex patterns."""
        try:
            logger.info("Starting UPI extraction from HTML content...")
            return self._extract_upi_from_text(html_content)

        except Exception as e:
            error_msg = f"Error extracting UPI from HTML: {str(e)}"
            logger.error(error_msg)
            return None

    async def extract_upi_from_iframe(self, frame: Any) -> Optional[str]:
        """Extract UPI IDs from iframe content using regex patterns."""
        try:
            logger.debug("Starting UPI extraction from iframe content...")
            html_content = await frame.content()
            return self._extract_upi_from_text(html_content)

        except Exception as e:
            error_msg = f"Error extracting UPI from iframe: {str(e)}"
            logger.error(error_msg)
            return None

    def extract_upi_from_qr(self, image_path: str) -> Optional[str]:
        """Extract UPI IDs from QR code in image."""
        try:
            logger.info(f"Starting UPI extraction from QR code: {image_path}")

            # Verify image exists
            if not os.path.exists(image_path):
                error_msg = f"Image file not found: {image_path}"
                logger.error(error_msg)
                return None

            # TODO Method 1: Try pyzbar first (usually more reliable)

            # Method 2: OpenCV QR detection with preprocessing
            qr_data = self._opencv_qr_detection(image_path)

            if qr_data:
                upi_id = self._extract_upi_from_text(qr_data)
                if upi_id:
                    return upi_id

            logger.warning(
                "No UPI IDs found in QR code after trying all detection methods"
            )
            return None

        except Exception as e:
            error_msg = f"Error extracting UPI from QR: {str(e)}"
            logger.error(error_msg)
            return None

    def _opencv_qr_detection(self, image_path: str) -> Optional[str]:
        """OpenCV-based QR detection with multiple preprocessing methods."""

        try:
            # Read the image
            image = cv2.imread(image_path)
            if image is None:
                raise Exception(f"Failed to load image from {image_path}")

            logger.debug(f"Processing image with shape: {image.shape}")
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

            # Multiple preprocessing techniques
            preprocessing_methods = [
                ("Original", gray),
                ("Gaussian Blur", cv2.GaussianBlur(gray, (3, 3), 0)),
                ("Median Blur", cv2.medianBlur(gray, 5)),
                (
                    "Adaptive Threshold - Gaussian",
                    cv2.adaptiveThreshold(
                        gray,
                        255,
                        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        cv2.THRESH_BINARY,
                        11,
                        2,
                    ),
                ),
                (
                    "Otsu's Threshold",
                    cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
                ),
            ]

            qr_detector = cv2.QRCodeDetector()

            # Try each preprocessing method with multiple scales
            for method_name, processed_image in preprocessing_methods:
                logger.debug(f"Trying {method_name} preprocessing...")

                for scale in [1.0, 1.5, 2.0, 0.7]:
                    if scale != 1.0:
                        h, w = processed_image.shape
                        new_w, new_h = int(w * scale), int(h * scale)
                        scaled_image = cv2.resize(
                            processed_image,
                            (new_w, new_h),
                            interpolation=cv2.INTER_CUBIC,
                        )
                    else:
                        scaled_image = processed_image

                    data, bbox, _ = qr_detector.detectAndDecode(scaled_image)

                    if data:
                        logger.info(
                            f"Found QR code with {method_name} (scale {scale}): {data}"
                        )
                        return data  # Return immediately on first success

            # TODO: Try pyzbar on preprocessed images if available

        except Exception as e:
            logger.error(f"OpenCV QR detection error: {e}")

        return None
