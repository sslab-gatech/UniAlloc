    pub fn clear(&mut self) {
        for i in 0..self.len {
            unsafe {
                self.elements[i].as_mut_ptr().drop_in_place();
            }
        }
        self.len = 0;
    }
