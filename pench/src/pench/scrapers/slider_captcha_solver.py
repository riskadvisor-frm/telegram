import cv2
import numpy as np

PIXELS_EXTENSION = 10


class PuzzleSolver:
    def __init__(self, piece_path, background_path):
        self.piece_path = piece_path
        self.background_path = background_path

    def get_position(self):
        template, x_inf, y_sup, y_inf = self.__piece_preprocessing()
        background = self.__background_preprocessing(y_sup, y_inf)

        res = cv2.matchTemplate(background, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
        top_left = max_loc

        origin = x_inf
        end = top_left[0] + PIXELS_EXTENSION

        return end - origin

    def __background_preprocessing(self, y_sup, y_inf):
        background = self.__sobel_operator(self.background_path)
        background = background[y_sup:y_inf, :]
        background = self.__extend_background_boundary(background)
        background = self.__to_grayscale(background)

        return background

    def __piece_preprocessing(self):
        img = self.__sobel_operator(self.piece_path)
        x, w, y, h = self.__crop_piece(img)
        template = img[y:h, x:w]

        template = self.__extend_template_boundary(template)
        template = self.__to_grayscale(template)

        return template, x, y, h

    def __crop_piece(self, img):
        white_rows = []
        white_columns = []
        r, c = img.shape

        for row in range(r):
            for x in img[row, :]:
                if x != 0:
                    white_rows.append(row)

        for column in range(c):
            for x in img[:, column]:
                if x != 0:
                    white_columns.append(column)

        if not white_columns or not white_rows:
            # Fallback if no edges found (shouldn't happen with good sobel)
            return 0, img.shape[1], 0, img.shape[0]

        x = white_columns[0]
        w = white_columns[-1]
        y = white_rows[0]
        h = white_rows[-1]

        return x, w, y, h

    def __extend_template_boundary(self, template):
        extra_border_y = np.zeros(
            (template.shape[0], PIXELS_EXTENSION), dtype=template.dtype
        )
        template = np.hstack((extra_border_y, template, extra_border_y))

        extra_border_x = np.zeros(
            (PIXELS_EXTENSION, template.shape[1]), dtype=template.dtype
        )
        template = np.vstack((extra_border_x, template, extra_border_x))

        return template

    def __extend_background_boundary(self, background):
        extra_border = np.zeros(
            (PIXELS_EXTENSION, background.shape[1]), dtype=background.dtype
        )
        return np.vstack((extra_border, background, extra_border))

    def __sobel_operator(self, img_path):
        scale = 1
        delta = 0
        ddepth = cv2.CV_16S

        img = cv2.imread(img_path, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"Could not read image from {img_path}")

        img = cv2.GaussianBlur(img, (3, 3), 0)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        grad_x = cv2.Sobel(
            gray,
            ddepth,
            1,
            0,
            ksize=3,
            scale=scale,
            delta=delta,
            borderType=cv2.BORDER_DEFAULT,
        )
        grad_y = cv2.Sobel(
            gray,
            ddepth,
            0,
            1,
            ksize=3,
            scale=scale,
            delta=delta,
            borderType=cv2.BORDER_DEFAULT,
        )
        abs_grad_x = cv2.convertScaleAbs(grad_x)
        abs_grad_y = cv2.convertScaleAbs(grad_y)
        grad = cv2.addWeighted(abs_grad_x, 0.5, abs_grad_y, 0.5, 0)

        return grad

    def __to_grayscale(self, img):
        # The input 'img' from sobel operator is already single channel (grayscale representation of edges).
        # The reference code wrote to file and read back as grayscale.
        # We can just ensure it is uint8 or return as is if it's already correct format.
        # cv2.addWeighted returns array with same type as input if unspec. abs_grad are uint8.
        return img
